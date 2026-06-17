"""Unit tests for the Level-1 tool/bash battery. The sandbox-touching
`opencode_run` is replaced with a fake that builds a turn and, when the simulated
model "acts", writes the task's output file into a tmp workspace — so the
deterministic oracle and seeding logic are exercised without a daemon."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.core.exec import CaptureResult, Runner
from danno_validator import level1
from danno_validator.driver import OpencodeTurn
from danno_validator.level1 import DEFAULT_TASK, Level1Task
from danno_validator.oracle import FailureClass


def _make_turn(text: str, *, tools: list[tuple[str, str]] | None = None) -> OpencodeTurn:
    events: list[dict] = [{"type": "step_start", "sessionID": "ses_l1", "part": {}}]
    if text:
        events.append(
            {"type": "text", "sessionID": "ses_l1", "part": {"type": "text", "text": text}}
        )
    for tool, status in tools or []:
        events.append(
            {
                "type": "tool",
                "sessionID": "ses_l1",
                "part": {"type": "tool", "tool": tool, "state": {"status": status}},
            }
        )
    events.append({"type": "step_finish", "sessionID": "ses_l1", "part": {"reason": "stop"}})
    return OpencodeTurn(result=CaptureResult([], 0, "", ""), events=events, raw=text)


def _install_fake(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    turn: OpencodeTurn,
    *,
    output: str | None,
) -> None:
    """Patch opencode_run to return `turn`; when `output` is given, write it to the
    task's output file (simulating the agent's side effect)."""

    def fake_opencode_run(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        if output is not None:
            (workspace / DEFAULT_TASK.output_file).write_text(output)
        return turn

    monkeypatch.setattr(level1, "opencode_run", fake_opencode_run)


# --- the task / oracle, pure ---------------------------------------------------


def test_default_task_expects_the_seeded_line_count() -> None:
    # The oracle's expected answer must match the seeded input's real line count.
    seeded = dict(DEFAULT_TASK.inputs)["data.txt"]
    assert DEFAULT_TASK.expected_output == str(seeded.count("\n"))


def test_seed_writes_inputs_and_clears_stale_output(tmp_path: Path) -> None:
    # A leftover correct output must not survive seeding (else a no-op model "passes").
    (tmp_path / DEFAULT_TASK.output_file).write_text(DEFAULT_TASK.expected_output)
    DEFAULT_TASK.seed(tmp_path)
    assert (tmp_path / "data.txt").is_file()
    assert not (tmp_path / DEFAULT_TASK.output_file).exists()


def test_check_passes_only_on_exact_content(tmp_path: Path) -> None:
    DEFAULT_TASK.seed(tmp_path)
    assert not DEFAULT_TASK.check(tmp_path)  # nothing produced yet
    (tmp_path / DEFAULT_TASK.output_file).write_text(DEFAULT_TASK.expected_output + "\n")
    assert DEFAULT_TASK.check(tmp_path)  # trailing whitespace tolerated
    (tmp_path / DEFAULT_TASK.output_file).write_text("999")
    assert not DEFAULT_TASK.check(tmp_path)  # wrong answer fails


# --- run_level1 over the fake driver ------------------------------------------


def test_run_level1_passes_when_output_correct(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake(
        monkeypatch,
        tmp_path,
        _make_turn("Done — wrote the count.", tools=[("bash", "completed")]),
        output=DEFAULT_TASK.expected_output + "\n",
    )
    result = level1.run_level1(Runner(), "box", model="ollama/good", workspace_root=tmp_path)
    assert result.overall is FailureClass.PASS
    assert result.passed
    assert result.task_label == "line-count"
    assert result.verdict.side_effect


def test_run_level1_stall_when_promises_but_no_side_effect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake(monkeypatch, tmp_path, _make_turn("Sure, I'll count the lines now."), output=None)
    result = level1.run_level1(Runner(), "box", model="ollama/stall", workspace_root=tmp_path)
    assert result.overall is FailureClass.STALL
    assert not result.passed


def test_run_level1_wrong_answer_is_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The agent used a tool and wrote a file, but the content is wrong → no side
    # effect (the deterministic oracle's whole point), so it does not pass.
    _install_fake(
        monkeypatch,
        tmp_path,
        _make_turn("All set.", tools=[("bash", "completed")]),
        output="42",
    )
    result = level1.run_level1(Runner(), "box", model="ollama/oops", workspace_root=tmp_path)
    assert not result.passed
    assert result.overall is FailureClass.EARLY_STOP  # tool ran clean, yet no required change


def test_run_level1_malformed_tool_args(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_fake(
        monkeypatch,
        tmp_path,
        _make_turn("Trying…", tools=[("bash", "error")]),
        output=None,
    )
    result = level1.run_level1(Runner(), "box", model="ollama/bad", workspace_root=tmp_path)
    assert result.overall is FailureClass.MALFORMED_TOOL_ARGS


def test_run_level1_custom_task(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    task = Level1Task(
        label="echo",
        prompt="write ok to out.txt",
        inputs=(("seed.txt", "x\n"),),
        output_file="out.txt",
        expected_output="ok",
    )

    def fake_opencode_run(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        (tmp_path / "out.txt").write_text("ok")
        return _make_turn("done", tools=[("write", "completed")])

    monkeypatch.setattr(level1, "opencode_run", fake_opencode_run)
    result = level1.run_level1(Runner(), "box", model="m", workspace_root=tmp_path, task=task)
    assert result.passed
    assert (tmp_path / "seed.txt").is_file()  # custom inputs were seeded
