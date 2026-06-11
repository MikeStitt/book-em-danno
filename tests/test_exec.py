from __future__ import annotations

import pytest

from book_em_danno.core import exec as exec_mod
from book_em_danno.core.exec import CommandNotFoundError, Runner, require_cmd


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


def test_require_cmd_found() -> None:
    assert require_cmd("python3") or require_cmd("python")


def test_require_cmd_missing_fails_loud() -> None:
    with pytest.raises(CommandNotFoundError):
        require_cmd("definitely-not-a-real-binary-xyz")
