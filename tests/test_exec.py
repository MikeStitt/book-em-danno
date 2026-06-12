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


def test_dry_run_never_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    Runner(apply=True, dry_run=True).advise(["echo", "hi"], why="greet")
    assert calls == []  # dry-run wins over apply


def test_apply_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    Runner(apply=True).advise(["echo", "hi"], why="greet")
    assert calls == [["echo", "hi"]]


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
