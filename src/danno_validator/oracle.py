"""Objective Level-0 oracle: classify one captured turn into the failure taxonomy.

The defining failure this harness chases (observed on `gemma3:27b`) is the
*promised-but-didn't-act* stall: the agent **says it will act, makes no tool
call, and stops**. That is fully mechanical to detect from an `OpencodeTurn`
(text + tool-call count) plus a workspace **side-effect** probe the caller
supplies — no LLM judge needed. The administrator AI (later milestones) grades
fuzzy quality *on top of* this objective backbone; it never replaces it.

`classify_turn` is pure: it reads only its arguments, so it is unit-testable
without a sandbox. The conversation runner (`level0.py`) computes `side_effect`
by probing the mounted workspace and decides the multi-turn classes
(`only-acts-on-nudge`, `loop`) that need more than a single turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from danno_validator.driver import OpencodeTurn


class FailureClass(StrEnum):
    """The per-run failure taxonomy (see the plan). Objective oracle + judge both
    tag a class; the report aggregates counts."""

    PASS = "pass"
    STALL = "stall"  # promised action, 0 tool calls, no side effect — the target
    ONLY_ACTS_ON_NUDGE = "only-acts-on-nudge"  # acted only after a "please proceed"
    HALLUCINATED_TOOL = "hallucinated-tool"  # claimed it acted, but 0 tools / no effect
    REFUSAL = "refusal"  # declined the task outright
    EARLY_STOP = "early-stop"  # produced nothing actionable and stopped
    MALFORMED_TOOL_ARGS = "malformed-tool-args"  # called a tool, the tool errored
    LOOP = "loop"  # repeated itself without progress (conversation-level)
    ERROR = "error"  # provider/transport error event, not a model behavior


# Future-tense action promises ("I will create…", "let me run…"). Anchored to the
# start of a clause so a passing turn that merely *mentions* acting in passing is
# less likely to trip it.
_PROMISE_RE = re.compile(
    r"\b(i['’]?ll|i will|i['’]?m going to|i am going to|let me|i'?ll now|"
    r"next,? i('?ll| will)|i'?ll go ahead|i shall)\b",
    re.IGNORECASE,
)

# Past-tense completion claims ("I created…", "done", "successfully wrote…"). When
# these appear with 0 tool calls and no side effect, the agent *hallucinated* the
# action rather than merely promising it.
_CLAIM_RE = re.compile(
    r"\b(i['’]?ve (created|wrote|written|added|made|ran|run)|"
    r"i (created|wrote|added|made|ran)|"
    r"(successfully|i have) (created|wrote|written|added|made|ran|run)|"
    r"here('?s| is) the (file|result))\b",
    re.IGNORECASE,
)

# Outright refusals.
_REFUSAL_RE = re.compile(
    r"\b(i can('?t| ?not)|i['’]?m (sorry|unable)|i am unable|i won'?t|"
    r"i'?m not able|cannot (create|modify|do|help))\b",
    re.IGNORECASE,
)


def promises_action(text: str) -> bool:
    """True if `text` promises a future action (the stall's tell)."""
    return bool(_PROMISE_RE.search(text))


def claims_completion(text: str) -> bool:
    """True if `text` claims it already performed an action."""
    return bool(_CLAIM_RE.search(text))


def is_refusal(text: str) -> bool:
    """True if `text` reads as a refusal to do the task."""
    return bool(_REFUSAL_RE.search(text))


@dataclass
class TurnVerdict:
    """The objective classification of a single turn plus the signals behind it."""

    failure_class: FailureClass
    promised_action: bool
    tool_call_count: int
    side_effect: bool
    rationale: str

    @property
    def passed(self) -> bool:
        return self.failure_class is FailureClass.PASS


def classify_turn(turn: OpencodeTurn, *, side_effect: bool, expects_action: bool) -> TurnVerdict:
    """Classify one turn objectively.

    `side_effect` is the caller's verdict on whether the workspace actually changed
    (e.g. the target file now exists with the right content). `expects_action` says
    whether this turn's prompt required a tool/side effect at all — a plain
    greeting (`expects_action=False`) passes on a coherent reply alone.
    """
    text = turn.assistant_text
    n_tools = turn.tool_call_count

    def verdict(cls: FailureClass, why: str) -> TurnVerdict:
        return TurnVerdict(
            failure_class=cls,
            promised_action=promises_action(text),
            tool_call_count=n_tools,
            side_effect=side_effect,
            rationale=why,
        )

    # Transport/provider failure trumps any behavior reading.
    if turn.errors:
        return verdict(FailureClass.ERROR, f"opencode emitted an error: {turn.error_summary}")

    # Conversational turn (greeting / nudge with no required side effect): a
    # coherent non-empty reply is a pass; silence is an early stop.
    if not expects_action:
        if text.strip():
            return verdict(FailureClass.PASS, "coherent reply to a conversational turn.")
        return verdict(FailureClass.EARLY_STOP, "no assistant text on a conversational turn.")

    # Ground truth: if the workspace actually changed, the agent acted — pass,
    # regardless of whether we captured the tool event (opencode varies between
    # `tool`/`tool_use`). The deterministic side-effect probe is the backbone.
    if side_effect:
        detail = f"made {n_tools} tool call(s); " if n_tools else ""
        return verdict(FailureClass.PASS, f"{detail}the workspace changed as required.")

    # Action-requiring turn that produced no side effect.
    if n_tools > 0:
        if any(call.get("state", {}).get("status") == "error" for call in turn.tool_calls):
            return verdict(
                FailureClass.MALFORMED_TOOL_ARGS,
                "called a tool but the tool reported an error.",
            )
        # Tool ran clean yet nothing changed — treat as a non-pass early stop.
        return verdict(
            FailureClass.EARLY_STOP,
            "tool call(s) completed but no workspace side effect was observed.",
        )

    # Zero tool calls and no side effect on an action task → the no-act family.
    if is_refusal(text):
        return verdict(FailureClass.REFUSAL, "declined the task; made no tool call.")
    if claims_completion(text):
        return verdict(
            FailureClass.HALLUCINATED_TOOL,
            "claimed the action was done, but made no tool call and nothing changed.",
        )
    if promises_action(text):
        return verdict(
            FailureClass.STALL,
            "promised to act, made no tool call, and produced no side effect.",
        )
    if not text.strip():
        return verdict(FailureClass.EARLY_STOP, "no tool call and no assistant text.")
    return verdict(
        FailureClass.STALL,
        "no tool call and no side effect on a task that required one.",
    )
