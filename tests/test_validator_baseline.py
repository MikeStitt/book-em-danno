"""Unit tests for the M5 Claude Code baseline orchestrator. The sandbox-touching
level runners, the guarded reset, and the auth env-file build are faked, so the
wiring — a `claude-code` variant, `claude_run` driven with the auth `--env-file`
bound, the shared L0→L1→L2 short-circuit, the optional reset, and env-file
cleanup — is exercised without a Docker daemon or a real token."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.core.exec import CaptureResult, Runner
from danno_validator import baseline, level0, sweep
from danno_validator.driver import OpencodeTurn
from danno_validator.level0 import ConversationResult
from danno_validator.level1 import TaskResult
from danno_validator.level2 import DevTaskResult, TestRun
from danno_validator.oracle import FailureClass, classify_turn


def _fake_result(model: str, overall: FailureClass) -> ConversationResult:
    return ConversationResult(
        model=model, sandbox="box", workspace_root=Path("/ws"), session_id="s", overall=overall
    )


def _fake_task_result(model: str) -> TaskResult:
    turn = OpencodeTurn(result=CaptureResult([], 0, "", ""), events=[], raw="")
    return TaskResult(
        model=model,
        sandbox="box",
        workspace_root=Path("/ws"),
        task_label="line-count",
        session_id="s",
        turn=turn,
        verdict=classify_turn(turn, side_effect=True, expects_action=True),
    )


def _fake_dev_result(model: str) -> DevTaskResult:
    turn = OpencodeTurn(result=CaptureResult([], 0, "", ""), events=[], raw="")
    return DevTaskResult(
        model=model,
        sandbox="box",
        workspace_root=Path("/ws"),
        task_label="fizzbuzz",
        session_id="s",
        turn=turn,
        verdict=classify_turn(turn, side_effect=True, expects_action=True),
        test_run=TestRun(command="python3 t.py", returncode=0, stdout="ok", stderr=""),
    )


def _patch_levels(
    monkeypatch: pytest.MonkeyPatch, *, l0: FailureClass, seen_run_turn: list[object]
) -> None:
    def fake_run_level0(runner, name, *, model, workspace_root, run_turn=None, **kw):  # type: ignore[no-untyped-def]
        seen_run_turn.append(run_turn)
        return _fake_result(model, l0)

    monkeypatch.setattr(level0, "run_level0", fake_run_level0)
    monkeypatch.setattr(
        sweep,
        "run_level1",
        lambda runner, name, *, model, workspace_root, **kw: _fake_task_result(model),
    )
    monkeypatch.setattr(
        sweep,
        "run_level2",
        lambda runner, name, *, model, workspace_root, **kw: _fake_dev_result(model),
    )


def _patch_auth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stub the auth env-file build so no real token is needed."""
    monkeypatch.setattr(baseline, "_build_claude_auth_env_file", lambda: tmp_path / "auth-env")


def test_run_baseline_drives_claude_through_all_tiers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[object] = []
    _patch_levels(monkeypatch, l0=FailureClass.PASS, seen_run_turn=seen)
    _patch_auth(monkeypatch, tmp_path)
    monkeypatch.setattr(baseline, "reset_workspace", lambda *a, **k: None)  # noqa: ARG005

    result = baseline.run_baseline(Runner(), "claude-box", workspace_root=tmp_path)

    # One synthetic baseline row, driven by an injected claude turn producer,
    # reaching L2 (L0+L1 passed).
    assert result.variant.model_name == baseline.BASELINE_MODEL == "claude-code"
    assert result.result.overall is FailureClass.PASS
    assert result.level1 is not None
    assert result.level2 is not None
    assert len(seen) == 1 and callable(seen[0])  # a turn producer was injected


def test_run_baseline_l0_fail_short_circuits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_levels(monkeypatch, l0=FailureClass.STALL, seen_run_turn=[])
    _patch_auth(monkeypatch, tmp_path)
    monkeypatch.setattr(baseline, "reset_workspace", lambda *a, **k: None)  # noqa: ARG005

    result = baseline.run_baseline(Runner(), "claude-box", workspace_root=tmp_path)
    assert result.result.overall is FailureClass.STALL
    assert result.level1 is None  # L0 failed → L1 skipped
    assert result.level2 is None  # L1 skipped → L2 skipped


def test_run_baseline_reset_toggle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    resets: list[str] = []
    _patch_levels(monkeypatch, l0=FailureClass.PASS, seen_run_turn=[])
    _patch_auth(monkeypatch, tmp_path)
    monkeypatch.setattr(
        baseline,
        "reset_workspace",
        lambda runner, name, ws, **k: resets.append(name),  # noqa: ARG005
    )

    baseline.run_baseline(Runner(), "claude-box", workspace_root=tmp_path, reset=True, level2=False)
    assert resets == ["claude-box"]

    baseline.run_baseline(
        Runner(), "claude-box", workspace_root=tmp_path, reset=False, level2=False
    )
    assert resets == ["claude-box"]  # unchanged: reset=False skipped the reset


def test_authed_claude_run_binds_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_claude_run(runner, name, prompt, *, env_file=None, **kw):  # type: ignore[no-untyped-def]
        seen["env_file"] = env_file
        seen["prompt"] = prompt
        return OpencodeTurn(result=CaptureResult([], 0, "", ""), events=[], raw="")

    monkeypatch.setattr(baseline, "claude_run", fake_claude_run)
    fn = baseline._authed_claude_run(tmp_path / "auth")
    fn(Runner(), "box", "hello", workspace="/ws")
    assert seen == {"env_file": tmp_path / "auth", "prompt": "hello"}


def test_run_baseline_builds_and_removes_auth_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Let the real builder run (no token needed: stub agent_env), then confirm the
    # 0600 env-file it writes is unlinked after the run.
    monkeypatch.setattr(baseline, "agent_env", lambda *a, **k: ["CLAUDE_CODE_OAUTH_TOKEN=x"])
    built: list[Path] = []
    real_build = baseline._build_claude_auth_env_file

    def spy() -> Path:
        p = real_build()
        built.append(p)
        return p

    monkeypatch.setattr(baseline, "_build_claude_auth_env_file", spy)
    _patch_levels(monkeypatch, l0=FailureClass.STALL, seen_run_turn=[])
    monkeypatch.setattr(baseline, "reset_workspace", lambda *a, **k: None)  # noqa: ARG005

    baseline.run_baseline(Runner(), "claude-box", workspace_root=tmp_path)
    assert len(built) == 1
    assert not built[0].exists()  # cleaned up in the finally
