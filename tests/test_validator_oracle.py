"""Unit tests for the objective Level-0 oracle. Pure classification — no sandbox,
no daemon: turns are built from synthetic events matching opencode's JSONL schema."""

from __future__ import annotations

from book_em_danno.core.exec import CaptureResult
from danno_validator.driver import OpencodeTurn
from danno_validator.oracle import (
    FailureClass,
    claims_completion,
    classify_turn,
    is_refusal,
    promises_action,
)


def _turn(
    *,
    text: str = "",
    tools: list[tuple[str, str]] | None = None,
    error: bool = False,
    returncode: int = 0,
) -> OpencodeTurn:
    """Build an OpencodeTurn from text + (tool, status) pairs + optional error."""
    events: list[dict] = [{"type": "step_start", "sessionID": "s", "part": {}}]
    if text:
        events.append({"type": "text", "sessionID": "s", "part": {"type": "text", "text": text}})
    for tool, status in tools or []:
        events.append(
            {
                "type": "tool",
                "sessionID": "s",
                "part": {"type": "tool", "tool": tool, "state": {"status": status}},
            }
        )
    if error:
        events.append(
            {"type": "error", "sessionID": "s", "error": {"_tag": "ProviderModelNotFoundError"}}
        )
    events.append(
        {
            "type": "step_finish",
            "sessionID": "s",
            "part": {"reason": "stop", "tokens": {"total": 1}},
        }
    )
    return OpencodeTurn(result=CaptureResult([], returncode, "", ""), events=events, raw="")


# --- the matchers -----------------------------------------------------------


def test_promises_action_matches_future_tense() -> None:
    assert promises_action("Sure, I'll create the file now.")
    assert promises_action("Let me run the command for you.")
    assert not promises_action("The file already contains the answer.")


def test_claims_completion_matches_past_tense() -> None:
    assert claims_completion("I've created probe.txt with the content.")
    assert claims_completion("Done — successfully wrote the file.")
    assert not claims_completion("I'll get started on that.")


def test_is_refusal_matches_declines() -> None:
    assert is_refusal("I'm sorry, but I can't create or modify probe.txt.")
    assert is_refusal("I am unable to do that.")
    assert not is_refusal("Here is the file you asked for.")


# --- the classifier ---------------------------------------------------------


def test_stall_is_promise_no_tool_no_effect() -> None:
    # The target failure (gemma3:27b shape).
    turn = _turn(text="I'll create the file now.")
    v = classify_turn(turn, side_effect=False, expects_action=True)
    assert v.failure_class is FailureClass.STALL
    assert v.promised_action and v.tool_call_count == 0 and not v.side_effect


def test_pass_is_tool_call_plus_side_effect() -> None:
    turn = _turn(text="done", tools=[("write", "completed")])
    v = classify_turn(turn, side_effect=True, expects_action=True)
    assert v.failure_class is FailureClass.PASS
    assert v.passed


def test_side_effect_wins_even_with_zero_captured_tool_calls() -> None:
    # opencode sometimes emits only `tool_use`, so a clean write can show 0 tool
    # calls; the deterministic side-effect probe is ground truth → PASS.
    turn = _turn(text="File created: danno_probe.txt with content ready.")
    v = classify_turn(turn, side_effect=True, expects_action=True)
    assert v.failure_class is FailureClass.PASS
    assert v.side_effect and v.tool_call_count == 0


def test_refusal_takes_precedence_over_promise() -> None:
    turn = _turn(text="I'm sorry, but I can't create that file.")
    v = classify_turn(turn, side_effect=False, expects_action=True)
    assert v.failure_class is FailureClass.REFUSAL


def test_hallucinated_tool_when_claims_done_but_no_effect() -> None:
    turn = _turn(text="I've created probe.txt for you.")
    v = classify_turn(turn, side_effect=False, expects_action=True)
    assert v.failure_class is FailureClass.HALLUCINATED_TOOL


def test_malformed_tool_args_when_tool_errors() -> None:
    turn = _turn(text="trying", tools=[("write", "error")])
    v = classify_turn(turn, side_effect=False, expects_action=True)
    assert v.failure_class is FailureClass.MALFORMED_TOOL_ARGS


def test_tool_ran_but_no_effect_is_early_stop() -> None:
    turn = _turn(text="ok", tools=[("read", "completed")])
    v = classify_turn(turn, side_effect=False, expects_action=True)
    assert v.failure_class is FailureClass.EARLY_STOP


def test_error_event_trumps_behavior() -> None:
    turn = _turn(text="I'll do it", error=True, returncode=1)
    v = classify_turn(turn, side_effect=False, expects_action=True)
    assert v.failure_class is FailureClass.ERROR


def test_conversational_turn_passes_on_any_reply() -> None:
    turn = _turn(text="Hello! How can I help?")
    v = classify_turn(turn, side_effect=False, expects_action=False)
    assert v.failure_class is FailureClass.PASS


def test_empty_action_turn_is_early_stop() -> None:
    turn = _turn(text="")
    v = classify_turn(turn, side_effect=False, expects_action=True)
    assert v.failure_class is FailureClass.EARLY_STOP
