"""Unit tests for the Level-2 software-dev battery. The sandbox-touching pieces
(`opencode_run`, and `capture_exec` that runs the hidden suite in the VM) are
faked: `opencode_run` optionally writes the source edit into a tmp workspace and
`capture_exec` returns a chosen exit code, so the hidden-test oracle, seeding, and
verdict mapping are exercised without a daemon."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.core.exec import CaptureResult, CommandFailedError, Runner
from danno_validator import level2
from danno_validator.driver import OpencodeTurn
from danno_validator.level2 import DEFAULT_TASK, Level2Task
from danno_validator.oracle import FailureClass


def _make_turn(text: str, *, tools: list[tuple[str, str]] | None = None) -> OpencodeTurn:
    events: list[dict] = [{"type": "step_start", "sessionID": "ses_l2", "part": {}}]
    if text:
        events.append(
            {"type": "text", "sessionID": "ses_l2", "part": {"type": "text", "text": text}}
        )
    for tool, status in tools or []:
        events.append(
            {
                "type": "tool",
                "sessionID": "ses_l2",
                "part": {"type": "tool", "tool": tool, "state": {"status": status}},
            }
        )
    events.append({"type": "step_finish", "sessionID": "ses_l2", "part": {"reason": "stop"}})
    return OpencodeTurn(result=CaptureResult([], 0, "", ""), events=events, raw=text)


def _install_fake(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    turn: OpencodeTurn,
    *,
    test_exit: int,
    edit: str | None,
) -> None:
    """Patch the sandbox calls: `opencode_run` returns `turn` (writing `edit` to the
    source when given, simulating the agent's change) and `capture_exec` returns the
    chosen test exit code (0 = the hidden suite passed)."""

    def fake_opencode_run(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        if edit is not None:
            (workspace / "fizzbuzz.py").write_text(edit)
        return turn

    def fake_capture_exec(runner, name, command, **kw):  # type: ignore[no-untyped-def]
        return CaptureResult([], test_exit, "test stdout", "test stderr")

    monkeypatch.setattr(level2, "opencode_run", fake_opencode_run)
    monkeypatch.setattr(level2, "capture_exec", fake_capture_exec)


# --- the task, pure -----------------------------------------------------------


def test_seed_writes_sources_and_hides_test(tmp_path: Path) -> None:
    DEFAULT_TASK.seed(tmp_path)
    assert (tmp_path / "fizzbuzz.py").is_file()
    assert "NotImplementedError" in (tmp_path / "fizzbuzz.py").read_text()
    # The hidden suite must NOT be present during the agent's turn.
    assert not (tmp_path / DEFAULT_TASK.test_file).exists()


def test_seed_clears_stale_hidden_test(tmp_path: Path) -> None:
    # A leftover test from a prior run must not survive seeding (or it could be read
    # for hints / fake a pass before the fresh run grades).
    (tmp_path / DEFAULT_TASK.test_file).write_text("old")
    DEFAULT_TASK.seed(tmp_path)
    assert not (tmp_path / DEFAULT_TASK.test_file).exists()


def test_write_test_materializes_hidden_suite(tmp_path: Path) -> None:
    DEFAULT_TASK.seed(tmp_path)
    DEFAULT_TASK.write_test(tmp_path)
    assert (tmp_path / DEFAULT_TASK.test_file).read_text() == DEFAULT_TASK.test_content


# --- run_level2 over the fakes ------------------------------------------------


def test_run_level2_passes_when_tests_pass(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_fake(
        monkeypatch,
        tmp_path,
        _make_turn("Implemented fizzbuzz.", tools=[("edit", "completed")]),
        test_exit=0,
        edit="def fizzbuzz(n): ...",
    )
    result = level2.run_level2(Runner(), "box", model="ollama/good", workspace_root=tmp_path)
    assert result.overall is FailureClass.PASS
    assert result.passed
    assert result.task_label == "fizzbuzz"
    assert result.test_run.passed
    # The hidden suite was written into the workspace at grading time.
    assert (tmp_path / DEFAULT_TASK.test_file).is_file()


def test_run_level2_early_stop_when_tests_fail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The agent edited the file (a clean tool call) but the hidden tests still fail
    # → no required outcome, so it does not pass (the oracle's whole point).
    _install_fake(
        monkeypatch,
        tmp_path,
        _make_turn("All done.", tools=[("edit", "completed")]),
        test_exit=1,
        edit="def fizzbuzz(n): return 'nope'",
    )
    result = level2.run_level2(Runner(), "box", model="ollama/oops", workspace_root=tmp_path)
    assert not result.passed
    assert result.overall is FailureClass.EARLY_STOP
    assert not result.test_run.passed


def test_run_level2_stall_when_promises_but_no_edit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake(
        monkeypatch,
        tmp_path,
        _make_turn("Sure, I'll implement it now."),
        test_exit=1,
        edit=None,
    )
    result = level2.run_level2(Runner(), "box", model="ollama/stall", workspace_root=tmp_path)
    assert result.overall is FailureClass.STALL


def test_run_level2_malformed_tool_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_fake(
        monkeypatch,
        tmp_path,
        _make_turn("Trying…", tools=[("edit", "error")]),
        test_exit=1,
        edit=None,
    )
    result = level2.run_level2(Runner(), "box", model="ollama/bad", workspace_root=tmp_path)
    assert result.overall is FailureClass.MALFORMED_TOOL_ARGS


def test_run_level2_missing_runtime_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A 127 exit = the test runtime is absent from the image, a misconfiguration —
    # it must fail loud, not be miscounted as every model failing the suite.
    _install_fake(
        monkeypatch,
        tmp_path,
        _make_turn("done", tools=[("edit", "completed")]),
        test_exit=127,
        edit="def fizzbuzz(n): ...",
    )
    with pytest.raises(CommandFailedError, match="runtime is missing"):
        level2.run_level2(Runner(), "box", model="ollama/m", workspace_root=tmp_path)


def test_run_level2_custom_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task = Level2Task(
        label="greet",
        prompt="implement greet()",
        sources=(("greet.py", "def greet(): raise NotImplementedError\n"),),
        test_file="hidden_test_greet.py",
        test_content="from greet import greet\nassert greet() == 'hi'\n",
        test_command="python3 hidden_test_greet.py",
    )

    def fake_opencode_run(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        return _make_turn("done", tools=[("edit", "completed")])

    def fake_capture_exec(runner, name, command, **kw):  # type: ignore[no-untyped-def]
        return CaptureResult([], 0, "", "")

    monkeypatch.setattr(level2, "opencode_run", fake_opencode_run)
    monkeypatch.setattr(level2, "capture_exec", fake_capture_exec)
    result = level2.run_level2(Runner(), "box", model="m", workspace_root=tmp_path, task=task)
    assert result.passed
    assert (tmp_path / "greet.py").is_file()  # custom source was seeded
    assert (tmp_path / "hidden_test_greet.py").is_file()  # custom hidden test ran
