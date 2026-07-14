from __future__ import annotations

import subprocess
import sys

import pytest

from book_em_danno.core import exec as exec_mod
from book_em_danno.core.exec import (
    CommandFailedError,
    CommandNotFoundError,
    Runner,
    require_cmd,
)


class _FakeProbe:
    """A fixed GateProbe for watchdog tests (stands in for the live capture tally)."""

    def __init__(self, *, calls: int = 0, tokens: int = 0) -> None:
        self._calls, self._tokens = calls, tokens

    def inference_calls(self) -> int:
        return self._calls

    def tokens(self) -> int:
        return self._tokens


def _patch_capture(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", stderr: str = "", returncode: int = 0
) -> list[list[str]]:
    """Stub subprocess.run to record the cmd and return a controlled CompletedProcess."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(exec_mod.subprocess, "run", fake_run)
    return calls


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


def test_capture_returns_streams_and_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout="out", stderr="err", returncode=3)
    res = Runner().capture(["echo", "hi"])
    assert (res.cmd, res.returncode, res.stdout, res.stderr) == (["echo", "hi"], 3, "out", "err")
    assert res.ok is False  # non-zero


def test_capture_ok_property_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout="hi", returncode=0)
    assert Runner().capture(["echo", "hi"]).ok is True


def test_capture_check_false_does_not_raise_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    # The harness-under-test exiting non-zero is data to inspect, not a danno error.
    _patch_capture(monkeypatch, returncode=1)
    assert Runner().capture(["opencode", "run"]).returncode == 1  # returned, did not raise


def test_capture_check_true_raises_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, returncode=1)
    with pytest.raises(CommandFailedError):
        Runner().capture(["git", "reset", "--hard"], check=True)


def test_capture_runs_regardless_of_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout="x")
    Runner(apply=False).capture(["echo", "hi"])  # not gated by --apply, like run()
    assert calls == [["echo", "hi"]]


def test_capture_uses_capture_output_and_text(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        exec_mod.subprocess,
        "run",
        lambda cmd, **kw: (captured.update(kw), subprocess.CompletedProcess(cmd, 0, "", ""))[1],
    )
    Runner().capture(["echo", "hi"])
    assert captured["capture_output"] is True and captured["text"] is True


def test_require_cmd_found() -> None:
    assert require_cmd("python3") or require_cmd("python")


def test_require_cmd_missing_fails_loud() -> None:
    with pytest.raises(CommandNotFoundError):
        require_cmd("definitely-not-a-real-binary-xyz")


# --- runaway-gate watchdog (M2), real subprocesses -----------------------------

_SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


def test_watchdog_timeout_kills_wedged_process() -> None:
    # Gate 3: a process that never returns is killed at timeout_s (no probe needed).
    runner = Runner()
    with runner.watching(timeout_s=0.5) as watch:
        res = runner.capture(_SLEEP)
    assert watch.breach is not None
    assert watch.breach.gate == "timeout"
    assert res.ok is False  # killed → non-zero exit


def test_watchdog_max_turns_kills_runaway_via_probe() -> None:
    # Gate 1: the live probe already exceeds max_turns → killed on the first poll.
    runner = Runner()
    with runner.watching(probe=_FakeProbe(calls=100), max_turns=10, timeout_s=30) as watch:
        runner.capture(_SLEEP)
    assert watch.breach is not None
    assert watch.breach.gate == "runaway"
    assert (watch.breach.observed, watch.breach.limit) == (100, 10)


def test_watchdog_max_tokens_kills_over_budget_via_probe() -> None:
    # Gate 2: the token tally exceeds max_tokens → over-budget kill.
    runner = Runner()
    with runner.watching(probe=_FakeProbe(tokens=5000), max_tokens=1000, timeout_s=30) as watch:
        runner.capture(_SLEEP)
    assert watch.breach is not None
    assert watch.breach.gate == "over-budget"


def test_watchdog_no_breach_completes_and_captures_output() -> None:
    # A well-behaved process finishes normally under the watchdog: no breach, output kept.
    runner = Runner()
    with runner.watching(probe=_FakeProbe(calls=1), max_turns=1000, timeout_s=30) as watch:
        res = runner.capture([sys.executable, "-c", "print('hello')"])
    assert watch.breach is None
    assert res.stdout.strip() == "hello"
    assert res.ok is True


def test_watching_context_restores_normal_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = Runner()
    with runner.watching(timeout_s=1):
        pass
    assert runner._watch is None  # restored on exit
    # after the block, capture() uses the normal (mockable) subprocess.run path again
    _patch_capture(monkeypatch, stdout="normal", returncode=0)
    assert runner.capture(["echo", "hi"]).stdout == "normal"
