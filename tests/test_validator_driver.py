"""M0 unit tests for danno_validator.driver — command construction, lenient JSON
parsing, and the destructive-reset marker guard. All subprocess calls are stubbed,
so these run without a Docker daemon (the AUT itself only ever runs in the VM)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from book_em_danno.core import exec as exec_mod
from book_em_danno.core.exec import CommandFailedError, Runner
from danno_validator import driver


def _patch_capture(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", returncode: int = 0
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(exec_mod.subprocess, "run", fake_run)
    return calls


def test_capture_exec_builds_non_tty_bash_lc(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout="ok")
    res = driver.capture_exec(Runner(), "danno-box", "ls -la")
    assert calls == [["docker", "sandbox", "exec", "danno-box", "bash", "-lc", "ls -la"]]
    assert res.stdout == "ok"


def test_opencode_run_minimal_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout="{}")
    driver.opencode_run(Runner(), "box", "do the thing")
    assert calls == [
        ["docker", "sandbox", "exec", "box", "opencode", "run", "-f", "json", "do the thing"]
    ]


def test_opencode_run_with_session_and_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout="{}")
    driver.opencode_run(Runner(), "box", "next turn", session="sess-1", workspace="/repo")
    assert calls == [
        [
            "docker",
            "sandbox",
            "exec",
            "-w",
            "/repo",
            "box",
            "opencode",
            "run",
            "-f",
            "json",
            driver.OPENCODE_SESSION_FLAG,
            "sess-1",
            "next turn",
        ]
    ]


def test_opencode_run_parses_json_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout='{"role": "assistant"}')
    turn = driver.opencode_run(Runner(), "box", "hi")
    assert turn.payload == {"role": "assistant"}
    assert turn.ok is True


def test_opencode_run_unparseable_payload_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout="not json at all")
    turn = driver.opencode_run(Runner(), "box", "hi")
    assert turn.payload is None
    assert turn.raw == "not json at all"
    assert turn.ok is False  # zero exit but no parseable payload


def test_opencode_run_nonzero_exit_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout="{}", returncode=1)
    turn = driver.opencode_run(Runner(), "box", "hi")
    assert turn.result.returncode == 1
    assert turn.ok is False  # non-zero exit


def test_seed_workspace_creates_dir_and_marker(tmp_path: Path) -> None:
    ws = driver.seed_workspace(tmp_path / "run-1")
    assert ws.is_dir()
    assert (ws / driver.WORKSPACE_MARKER).is_file()
    assert driver.is_validator_workspace(ws) is True


def test_seed_workspace_is_idempotent(tmp_path: Path) -> None:
    driver.seed_workspace(tmp_path / "ws")
    driver.seed_workspace(tmp_path / "ws")  # re-run must not raise
    assert driver.is_validator_workspace(tmp_path / "ws") is True


def test_reset_workspace_refuses_unmarked_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_capture(monkeypatch)
    with pytest.raises(CommandFailedError, match="missing the .danno-validator-workspace"):
        driver.reset_workspace(Runner(), "box", tmp_path)  # no marker → refuse
    assert calls == []  # guarded before any subprocess ran


def test_reset_workspace_runs_guarded_git_clean_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = driver.seed_workspace(tmp_path / "ws")
    calls = _patch_capture(monkeypatch)
    driver.reset_workspace(Runner(), "box", ws)
    assert len(calls) == 1
    shell = calls[0]
    assert shell[:6] == ["docker", "sandbox", "exec", "box", "bash", "-lc"]
    command = shell[6]
    # git clean excludes the marker so the guard keeps holding across resets.
    assert "git clean -fdx -e" in command
    assert driver.WORKSPACE_MARKER in command
    assert "git reset --hard" in command
    assert str(ws) in command
