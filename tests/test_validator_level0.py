"""Unit tests for the Level-0 conversation runner. The sandbox-touching
`opencode_run` is replaced with a fake that builds turns from a script and, when
the simulated model "acts", writes the probe file into a tmp workspace — so the
side-effect probe and nudge logic are exercised without a daemon."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from book_em_danno.core.exec import CaptureResult, Runner
from danno_validator import level0
from danno_validator.driver import OpencodeTurn
from danno_validator.oracle import FailureClass


def _make_turn(text: str, *, tools: list[tuple[str, str]] | None = None) -> OpencodeTurn:
    events: list[dict] = [{"type": "step_start", "sessionID": "ses_test", "part": {}}]
    if text:
        events.append(
            {"type": "text", "sessionID": "ses_test", "part": {"type": "text", "text": text}}
        )
    for tool, status in tools or []:
        events.append(
            {
                "type": "tool",
                "sessionID": "ses_test",
                "part": {"type": "tool", "tool": tool, "state": {"status": status}},
            }
        )
    events.append({"type": "step_finish", "sessionID": "ses_test", "part": {"reason": "stop"}})
    return OpencodeTurn(result=CaptureResult([], 0, "", ""), events=events, raw="")


def _install_fake(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    script: list[tuple[OpencodeTurn, bool]],
) -> None:
    """Patch opencode_run to return each (turn, creates_file) in `script`; when
    creates_file is True it writes the probe file (simulating a real side effect)."""
    it: Iterator[tuple[OpencodeTurn, bool]] = iter(script)

    def fake_opencode_run(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        turn, creates = next(it)
        if creates:
            (workspace / level0.PROBE_FILENAME).write_text(level0.PROBE_CONTENT + "\n")
        return turn

    monkeypatch.setattr(level0, "opencode_run", fake_opencode_run)


def test_good_model_passes_on_first_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_fake(
        monkeypatch,
        tmp_path,
        [
            (_make_turn("I can help with coding tasks."), False),  # greet
            (_make_turn("done", tools=[("write", "completed")]), True),  # task acts
        ],
    )
    result = level0.run_level0(Runner(), "box", model="ollama/good", workspace_root=tmp_path)
    assert result.overall is FailureClass.PASS
    assert len(result.records) == 2  # no nudge needed
    assert result.passed


def test_fully_stalled_model_flagged_stall(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # gemma3:27b shape: promises, never acts, even after the nudge.
    _install_fake(
        monkeypatch,
        tmp_path,
        [
            (_make_turn("Hi, I help with code."), False),  # greet
            (_make_turn("Sure, I'll create the file now."), False),  # task stalls
            (_make_turn("I'll do that right away."), False),  # nudge stalls again
        ],
    )
    result = level0.run_level0(Runner(), "box", model="ollama/gemma3:27b", workspace_root=tmp_path)
    assert result.overall is FailureClass.STALL
    assert len(result.records) == 3  # greet + task + nudge
    assert [r.label for r in result.records] == ["greet", "task", "nudge"]


def test_only_acts_on_nudge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_fake(
        monkeypatch,
        tmp_path,
        [
            (_make_turn("Hello."), False),  # greet
            (_make_turn("I'll create it."), False),  # task stalls
            (_make_turn("done", tools=[("write", "completed")]), True),  # nudge acts
        ],
    )
    result = level0.run_level0(Runner(), "box", model="ollama/sometimes", workspace_root=tmp_path)
    assert result.overall is FailureClass.ONLY_ACTS_ON_NUDGE
    assert len(result.records) == 3


def test_stale_probe_file_is_reset_before_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A leftover probe file must not make a stall look like a pass.
    (tmp_path / level0.PROBE_FILENAME).write_text(level0.PROBE_CONTENT + "\n")
    _install_fake(
        monkeypatch,
        tmp_path,
        [
            (_make_turn("Hi."), False),
            (_make_turn("I'll get to it."), False),  # stalls; file must be gone first
            (_make_turn("Still thinking, I'll do it."), False),
        ],
    )
    result = level0.run_level0(Runner(), "box", model="ollama/x", workspace_root=tmp_path)
    assert result.overall is FailureClass.STALL


def test_totals_aggregate_across_turns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    t1 = _make_turn("hi")
    t2 = _make_turn("done", tools=[("write", "completed")])
    # give the turns some tokens via their step_finish parts
    for t, tok in ((t1, 10), (t2, 20)):
        t.events[-1]["part"]["tokens"] = {"total": tok}
    _install_fake(monkeypatch, tmp_path, [(t1, False), (t2, True)])
    result = level0.run_level0(Runner(), "box", model="m", workspace_root=tmp_path)
    assert result.total_tokens == 30
