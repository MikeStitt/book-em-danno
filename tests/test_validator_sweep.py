"""Unit tests for the M2 sweep orchestrator. The sandbox-touching pieces
(`run_level0`, `reset_workspace`) and host setup (`generate`, `seed_workspace`)
are faked, so the iteration / reset / git-setup logic is exercised without a
Docker daemon or a real git repo."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.config.schema import DannoConfig, Model, OllamaBackend
from book_em_danno.core.exec import CaptureResult, Runner
from danno_validator import level0, sweep
from danno_validator.driver import OpencodeTurn
from danno_validator.level0 import ConversationResult
from danno_validator.level1 import TaskResult
from danno_validator.level2 import DevTaskResult, TestRun
from danno_validator.oracle import FailureClass, classify_turn


def _config() -> DannoConfig:
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1")
        },
        models={
            "alpha": Model(
                backend="ollama", tag="alpha:1b", context_budget=32000, output_limit=8192
            ),
            "beta": Model(backend="ollama", tag="beta:1b", context_budget=32000, output_limit=8192),
        },
    )


def _fake_result(model: str, overall: FailureClass) -> ConversationResult:
    return ConversationResult(
        model=model, sandbox="box", workspace_root=Path("/ws"), session_id="s", overall=overall
    )


def _fake_task_result(model: str) -> TaskResult:
    turn = OpencodeTurn(result=CaptureResult([], 0, "", ""), events=[], raw="")
    verdict = classify_turn(turn, side_effect=True, expects_action=True)
    return TaskResult(
        model=model,
        sandbox="box",
        workspace_root=Path("/ws"),
        task_label="line-count",
        session_id="s",
        turn=turn,
        verdict=verdict,
    )


def _fake_dev_result(model: str) -> DevTaskResult:
    turn = OpencodeTurn(result=CaptureResult([], 0, "", ""), events=[], raw="")
    verdict = classify_turn(turn, side_effect=True, expects_action=True)
    return DevTaskResult(
        model=model,
        sandbox="box",
        workspace_root=Path("/ws"),
        task_label="fizzbuzz",
        session_id="s",
        turn=turn,
        verdict=verdict,
        test_run=TestRun(command="python3 t.py", returncode=0, stdout="ok", stderr=""),
    )


def test_run_sweep_iterates_variants_resetting_each(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[str] = []

    def fake_reset(runner, name, workspace, **kw):  # type: ignore[no-untyped-def]
        calls.append(f"reset:{name}")
        return CaptureResult([], 0, "", "")

    def fake_run_level0(runner, name, *, model, workspace_root, **kw):  # type: ignore[no-untyped-def]
        calls.append(f"run:{model}")
        overall = FailureClass.PASS if "alpha" in model else FailureClass.STALL
        return _fake_result(model, overall)

    def fake_run_level1(runner, name, *, model, workspace_root, **kw):  # type: ignore[no-untyped-def]
        calls.append(f"l1:{model}")
        return _fake_task_result(model)

    def fake_run_level2(runner, name, *, model, workspace_root, **kw):  # type: ignore[no-untyped-def]
        calls.append(f"l2:{model}")
        return _fake_dev_result(model)

    monkeypatch.setattr(sweep, "reset_workspace", fake_reset)
    monkeypatch.setattr(level0, "run_level0", fake_run_level0)
    monkeypatch.setattr(sweep, "run_level1", fake_run_level1)
    monkeypatch.setattr(sweep, "run_level2", fake_run_level2)

    results = sweep.run_sweep(Runner(), "box", config=_config(), workspace_root=tmp_path)

    # A reset precedes each variant; the L0→L1→L2 chain short-circuits — alpha passes
    # L0 then L1 so it reaches L2; beta stalls at L0 so it runs no higher tier.
    assert calls == [
        "reset:box",
        "run:ollama/alpha:1b",
        "l1:ollama/alpha:1b",
        "l2:ollama/alpha:1b",
        "reset:box",
        "run:ollama/beta:1b",
    ]
    assert [s.variant.model_name for s in results] == ["alpha", "beta"]
    assert results[0].result.overall is FailureClass.PASS
    assert results[0].level1 is not None  # L0 passed → L1 ran
    assert results[0].level2 is not None  # L1 passed → L2 ran
    assert results[1].result.overall is FailureClass.STALL
    assert results[1].level1 is None  # L0 failed → L1 skipped
    assert results[1].level2 is None  # L1 skipped → L2 skipped


def test_run_sweep_level1_false_skips_tier1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ran_l1: list[str] = []
    monkeypatch.setattr(sweep, "reset_workspace", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(
        level0,
        "run_level0",
        lambda runner, name, *, model, workspace_root, **kw: _fake_result(model, FailureClass.PASS),
    )
    monkeypatch.setattr(sweep, "run_level1", lambda *a, **k: ran_l1.append("x"))  # noqa: ARG005

    results = sweep.run_sweep(
        Runner(), "box", config=_config(), workspace_root=tmp_path, level1=False
    )
    assert ran_l1 == []  # tier-1 disabled even though L0 passed
    assert all(s.level1 is None for s in results)
    assert all(s.level2 is None for s in results)  # L1 skipped → L2 skipped too


def test_run_sweep_level2_false_skips_tier2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ran_l2: list[str] = []
    monkeypatch.setattr(sweep, "reset_workspace", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(
        level0,
        "run_level0",
        lambda runner, name, *, model, workspace_root, **kw: _fake_result(model, FailureClass.PASS),
    )
    monkeypatch.setattr(
        sweep,
        "run_level1",
        lambda runner, name, *, model, workspace_root, **kw: _fake_task_result(model),
    )
    monkeypatch.setattr(sweep, "run_level2", lambda *a, **k: ran_l2.append("x"))  # noqa: ARG005

    results = sweep.run_sweep(
        Runner(), "box", config=_config(), workspace_root=tmp_path, level2=False
    )
    assert ran_l2 == []  # tier-2 disabled even though L0 and L1 passed
    assert all(s.level1 is not None for s in results)  # L1 still ran
    assert all(s.level2 is None for s in results)


def test_run_sweep_reset_false_skips_reset(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    resets: list[str] = []
    monkeypatch.setattr(
        sweep,
        "reset_workspace",
        lambda *a, **k: resets.append("x"),  # noqa: ARG005
    )
    monkeypatch.setattr(
        level0,
        "run_level0",
        lambda runner, name, *, model, workspace_root, **kw: _fake_result(model, FailureClass.PASS),
    )
    monkeypatch.setattr(
        sweep,
        "run_level1",
        lambda runner, name, *, model, workspace_root, **kw: _fake_task_result(model),
    )

    sweep.run_sweep(
        Runner(), "box", config=_config(), workspace_root=tmp_path, reset=False, level2=False
    )
    assert resets == []


def test_run_sweep_only_restricts_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sweep, "reset_workspace", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(
        level0,
        "run_level0",
        lambda runner, name, *, model, workspace_root, **kw: _fake_result(model, FailureClass.PASS),
    )
    monkeypatch.setattr(
        sweep,
        "run_level1",
        lambda runner, name, *, model, workspace_root, **kw: _fake_task_result(model),
    )
    results = sweep.run_sweep(
        Runner(), "box", config=_config(), workspace_root=tmp_path, only=["beta"], level2=False
    )
    assert [s.variant.model_name for s in results] == ["beta"]


class _RecordingRunner:
    """Captures the commands `prepare_workspace` would run on the host."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def capture(self, cmd, **kw):  # type: ignore[no-untyped-def]
        self.commands.append(cmd)
        return CaptureResult(cmd, 0, "", "")


def test_prepare_workspace_seeds_generates_and_commits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seeded: list[Path] = []
    generated: list[tuple[Path, bool]] = []
    monkeypatch.setattr(sweep, "seed_workspace", lambda p: seeded.append(p) or p)
    monkeypatch.setattr(
        sweep,
        "generate",
        lambda config, target, apply, disable_title=False: generated.append(  # noqa: ARG005
            (target, disable_title)
        ),
    )

    runner = _RecordingRunner()
    sweep.prepare_workspace(runner, tmp_path, _config())  # type: ignore[arg-type]

    assert seeded == [tmp_path]
    # The sweep config disables opencode's title generator (disable_title=True) so a
    # throwaway battery never spends the local default model on naming threads.
    assert generated == [(tmp_path, True)]
    # Three host git commands, in order, all scoped to the workspace with `-C`.
    assert len(runner.commands) == 3
    assert all(c[:3] == ["git", "-C", str(tmp_path)] for c in runner.commands)
    assert runner.commands[0][3] == "init"
    assert runner.commands[1][3] == "add"
    commit = runner.commands[2]
    assert "commit" in commit
    # The seed commit carries an inline author so it never needs host git config.
    assert "user.name=danno-validator" in commit
    assert "user.email=danno-validator@local" in commit
