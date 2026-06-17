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
from danno_validator.level0 import ConversationResult
from danno_validator.oracle import FailureClass


def _config() -> DannoConfig:
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1")
        },
        models={
            "alpha": Model(backend="ollama", tag="alpha:1b", tool_call=True),
            "beta": Model(backend="ollama", tag="beta:1b", tool_call=True),
        },
    )


def _fake_result(model: str, overall: FailureClass) -> ConversationResult:
    return ConversationResult(
        model=model, sandbox="box", workspace_root=Path("/ws"), session_id="s", overall=overall
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

    monkeypatch.setattr(sweep, "reset_workspace", fake_reset)
    monkeypatch.setattr(level0, "run_level0", fake_run_level0)

    results = sweep.run_sweep(Runner(), "box", config=_config(), workspace_root=tmp_path)

    # A reset precedes each run, in matrix (sorted) order.
    assert calls == ["reset:box", "run:ollama/alpha:1b", "reset:box", "run:ollama/beta:1b"]
    assert [s.variant.model_name for s in results] == ["alpha", "beta"]
    assert results[0].result.overall is FailureClass.PASS
    assert results[1].result.overall is FailureClass.STALL


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

    sweep.run_sweep(Runner(), "box", config=_config(), workspace_root=tmp_path, reset=False)
    assert resets == []


def test_run_sweep_only_restricts_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sweep, "reset_workspace", lambda *a, **k: None)  # noqa: ARG005
    monkeypatch.setattr(
        level0,
        "run_level0",
        lambda runner, name, *, model, workspace_root, **kw: _fake_result(model, FailureClass.PASS),
    )
    results = sweep.run_sweep(
        Runner(), "box", config=_config(), workspace_root=tmp_path, only=["beta"]
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
    generated: list[Path] = []
    monkeypatch.setattr(sweep, "seed_workspace", lambda p: seeded.append(p) or p)
    monkeypatch.setattr(
        sweep,
        "generate",
        lambda config, target, apply: generated.append(target),  # noqa: ARG005
    )

    runner = _RecordingRunner()
    sweep.prepare_workspace(runner, tmp_path, _config())  # type: ignore[arg-type]

    assert seeded == [tmp_path]
    assert generated == [tmp_path]
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
