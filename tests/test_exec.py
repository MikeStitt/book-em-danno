from __future__ import annotations

import subprocess

import pytest

from book_em_danno.core import exec as exec_mod
from book_em_danno.core.exec import (
    CommandFailedError,
    CommandNotFoundError,
    Runner,
    require_cmd,
)


def _patch_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(exec_mod.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    return calls


def test_advise_default_prints_but_does_not_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    cmd = Runner().advise(["echo", "hi"], why="greet")
    assert cmd == ["echo", "hi"]
    assert calls == []  # advise-by-default: nothing ran


def test_apply_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    Runner(apply=True).advise(["echo", "hi"], why="greet")
    assert calls == [["echo", "hi"]]


def test_run_always_executes_regardless_of_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    cmd = Runner(apply=False).run(["echo", "hi"], why="greet")
    assert cmd == ["echo", "hi"]
    assert calls == [["echo", "hi"]]  # run() is for terminal actions: not gated


def test_run_failure_raises_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd, **kw):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(exec_mod.subprocess, "run", boom)
    with pytest.raises(CommandFailedError):
        Runner(apply=False).run(["docker", "sandbox", "exec", "ghost", "bash"], why="shell")


def test_run_interactive_nonzero_exit_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    # An interactive launch/shell uses check=False: a non-zero exit from the user
    # quitting the TUI (or declining a prompt) must NOT become a danno error.
    monkeypatch.setattr(
        exec_mod.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)
    )
    exec_cmd = ["docker", "sandbox", "exec", "-it", "x", "claude"]
    cmd = Runner().run(exec_cmd, why="launch", check=False)
    assert cmd == exec_cmd  # returned, did not raise


def test_apply_forwards_cwd_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_mod.subprocess, "run", lambda cmd, **kw: captured.update(kw))
    Runner(apply=True).advise(
        ["bash", "install.sh"], why="run", cwd="/tmp/x", env={"ADOS_SOURCE_DIR": "/src"}
    )
    assert captured["cwd"] == "/tmp/x"
    assert captured["env"] == {"ADOS_SOURCE_DIR": "/src"}


def test_advise_default_cwd_env_are_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(exec_mod.subprocess, "run", lambda cmd, **kw: captured.update(kw))
    Runner(apply=True).advise(["echo", "hi"], why="greet")
    assert captured["cwd"] is None and captured["env"] is None


def test_apply_failure_raises_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd, **kw):  # type: ignore[no-untyped-def]
        raise subprocess.CalledProcessError(125, cmd)

    monkeypatch.setattr(exec_mod.subprocess, "run", boom)
    with pytest.raises(CommandFailedError):
        Runner(apply=True).advise(["docker", "sandbox", "rm", "ghost"], why="remove")


def test_require_cmd_found() -> None:
    assert require_cmd("python3") or require_cmd("python")


def test_require_cmd_missing_fails_loud() -> None:
    with pytest.raises(CommandNotFoundError):
        require_cmd("definitely-not-a-real-binary-xyz")
