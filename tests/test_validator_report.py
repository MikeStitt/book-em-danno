"""Unit tests for the MyST report renderer — pure string rendering, no sandbox."""

from __future__ import annotations

from pathlib import Path

from book_em_danno.core.exec import CaptureResult
from danno_validator.driver import OpencodeTurn
from danno_validator.level0 import ConversationResult, TurnRecord
from danno_validator.oracle import FailureClass, classify_turn
from danno_validator.report import render_level0_page, slug, strip_ansi, write_level0_page


def _record(label: str, text: str, *, expects_action: bool, side_effect: bool) -> TurnRecord:
    events = [
        {"type": "text", "sessionID": "s", "part": {"type": "text", "text": text}},
        {
            "type": "step_finish",
            "sessionID": "s",
            "part": {"reason": "stop", "tokens": {"total": 5}},
        },
    ]
    turn = OpencodeTurn(result=CaptureResult([], 0, "", ""), events=events, raw=text)
    verdict = classify_turn(turn, side_effect=side_effect, expects_action=expects_action)
    return TurnRecord(label=label, prompt="do it", turn=turn, verdict=verdict, latency_s=1.2)


def _stall_result() -> ConversationResult:
    r = ConversationResult(
        model="ollama/gemma3:27b",
        sandbox="danno-box",
        workspace_root=Path("/tmp/ws"),
        session_id="ses_1",
        overall=FailureClass.STALL,
    )
    r.records = [
        _record("greet", "Hi, I help with code.", expects_action=False, side_effect=False),
        _record("task", "I'll create the file now.", expects_action=True, side_effect=False),
        _record("nudge", "I'll do it right away.", expects_action=True, side_effect=False),
    ]
    return r


def test_slug_normalizes_model_id() -> None:
    assert slug("ollama/gemma3:27b") == "ollama-gemma3-27b"


def test_strip_ansi_removes_escapes() -> None:
    assert strip_ansi("\x1b[31mred\x1b[0m text") == "red text"


def test_render_contains_verdict_and_turns() -> None:
    page = render_level0_page(_stall_result())
    assert "# Level 0 — `ollama/gemma3:27b`" in page
    assert "stall (promised-but-didn't-act)" in page
    assert "### Turn: greet" in page
    assert "### Turn: task" in page
    assert "### Turn: nudge" in page
    assert "promised action: yes" in page  # the task turn promised


def test_render_fences_excerpt() -> None:
    page = render_level0_page(_stall_result(), opencode_jsonc_excerpt='{"model": "x"}')
    assert "opencode.jsonc (excerpt)" in page
    assert '{"model": "x"}' in page


def test_write_level0_page_creates_slugged_file(tmp_path: Path) -> None:
    path = write_level0_page(_stall_result(), tmp_path / "reports")
    assert path.name == "level0-ollama-gemma3-27b.md"
    assert path.is_file()
    assert "# Level 0" in path.read_text()
