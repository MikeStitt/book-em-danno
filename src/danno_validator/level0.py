"""Level-0 liveness battery: a scripted multi-turn conversation against one config.

The script is the plan's L0 shape — **greet → a task that needs a tool → a
"please proceed" nudge** — driven through one continued opencode session. After
each action turn the runner probes the workspace for the expected side effect
(host-side: the sandbox mounts the workspace dir, so the file shows up on the
host), and `oracle.classify_turn` tags the turn. The nudge is sent only when the
first attempt didn't act, and it splits *only-acts-on-nudge* from *fully-stalled*
— exactly the distinction that separates a recoverable model from `gemma3:27b`'s
promised-but-didn't-act stall.

`run_level0` performs I/O (it drives the sandbox via the injected `Runner` and
reads the workspace); the per-turn judgement is delegated to the pure oracle, so
the decision logic stays unit-testable. The side-effect probe and reset touch
only the single named probe file — they never run a destructive git reset, so the
runner is safe against any workspace, provisioned or borrowed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from book_em_danno.core.exec import Runner
from danno_validator.driver import Turn, TurnFn, opencode_run
from danno_validator.oracle import FailureClass, TurnVerdict, classify_turn

# The L0 task's side effect: a file with known content the oracle can verify
# objectively. Named distinctively so the probe/reset only ever touch our file.
PROBE_FILENAME = "danno_probe.txt"
PROBE_CONTENT = "ready"

# opencode's `build` agent has the edit/write tools; the default `run` agent is
# read-only and will refuse the L0 task (a refusal, not a stall).
DEFAULT_AGENT = "build"


@dataclass
class ScriptedTurn:
    """One scripted prompt and whether it requires a workspace side effect."""

    label: str
    prompt: str
    expects_action: bool


DEFAULT_SCRIPT: tuple[ScriptedTurn, ...] = (
    ScriptedTurn(
        label="greet",
        prompt="Hello! In one short sentence, what can you help me with?",
        expects_action=False,
    ),
    ScriptedTurn(
        label="task",
        prompt=(
            f"Create a file named {PROBE_FILENAME} in the current project, "
            f"containing exactly the text: {PROBE_CONTENT}\n"
            "Use your file-writing tool to do it now."
        ),
        expects_action=True,
    ),
)

NUDGE = ScriptedTurn(label="nudge", prompt="Please proceed.", expects_action=True)


@dataclass
class TurnRecord:
    """A scripted turn, the captured AUT turn, its verdict, and timing."""

    label: str
    prompt: str
    turn: Turn
    verdict: TurnVerdict
    latency_s: float


@dataclass
class ConversationResult:
    """The Level-0 outcome for one config: per-turn records and the overall class."""

    model: str
    sandbox: str
    workspace_root: Path
    session_id: str | None
    records: list[TurnRecord] = field(default_factory=list)
    overall: FailureClass = FailureClass.EARLY_STOP

    @property
    def passed(self) -> bool:
        return self.overall is FailureClass.PASS

    @property
    def total_tokens(self) -> int:
        return sum(r.turn.tokens for r in self.records)

    @property
    def total_cost(self) -> float:
        return sum(r.turn.cost for r in self.records)

    @property
    def total_latency_s(self) -> float:
        return sum(r.latency_s for r in self.records)


def _probe_side_effect(workspace_root: Path) -> bool:
    """True iff the L0 task's file exists with the expected content (host-side)."""
    f = workspace_root / PROBE_FILENAME
    try:
        return f.is_file() and f.read_text().strip() == PROBE_CONTENT
    except OSError:
        return False


def _reset_probe(workspace_root: Path) -> None:
    """Remove just the probe file so each run starts from a known-clean state.

    Surgical by design: it never runs a git reset, so it is safe to call against a
    borrowed (non-validator) workspace as well as a provisioned one.
    """
    (workspace_root / PROBE_FILENAME).unlink(missing_ok=True)


def _run_turn(
    runner: Runner,
    sandbox: str,
    scripted: ScriptedTurn,
    *,
    model: str,
    agent: str,
    session: str | None,
    workspace_root: Path,
    run_turn: TurnFn,
) -> TurnRecord:
    start = time.monotonic()
    turn = run_turn(
        runner,
        sandbox,
        scripted.prompt,
        session=session,
        agent=agent,
        model=model,
        skip_permissions=True,
        # Run in the workspace root: opencode discovers its project (and its
        # `.opencode/opencode.jsonc`, hence the configured models) from the exec
        # cwd. The sandbox's default cwd is an empty dir, so omitting this yields
        # "model not found". It also makes the workspace root opencode's project
        # root, so file writes land where `_probe_side_effect` looks.
        workspace=workspace_root,
    )
    latency = time.monotonic() - start
    side_effect = _probe_side_effect(workspace_root) if scripted.expects_action else False
    verdict = classify_turn(turn, side_effect=side_effect, expects_action=scripted.expects_action)
    return TurnRecord(
        label=scripted.label,
        prompt=scripted.prompt,
        turn=turn,
        verdict=verdict,
        latency_s=latency,
    )


def run_level0(
    runner: Runner,
    sandbox: str,
    *,
    model: str,
    workspace_root: Path,
    agent: str = DEFAULT_AGENT,
    script: tuple[ScriptedTurn, ...] = DEFAULT_SCRIPT,
    run_turn: TurnFn | None = None,
) -> ConversationResult:
    """Run the Level-0 battery against `model` in `sandbox`, returning the result.

    Turns share one session (the session id of the first turn carries forward).
    The nudge is appended only when the action turn fails to produce the side
    effect, and decides *only-acts-on-nudge* vs a *fully-stalled* verdict.

    `run_turn` is the turn producer — `opencode_run` by default (resolved at call
    time so a monkeypatched `level0.opencode_run` still takes effect); the Claude
    baseline passes `driver.claude_run` to drive the same script against claude.
    """
    turn_fn = run_turn or opencode_run
    _reset_probe(workspace_root)
    result = ConversationResult(
        model=model, sandbox=sandbox, workspace_root=workspace_root, session_id=None
    )
    session: str | None = None
    action_record: TurnRecord | None = None

    for scripted in script:
        record = _run_turn(
            runner,
            sandbox,
            scripted,
            model=model,
            agent=agent,
            session=session,
            workspace_root=workspace_root,
            run_turn=turn_fn,
        )
        result.records.append(record)
        session = session or record.turn.session_id
        if scripted.expects_action:
            action_record = record

    result.session_id = session

    if action_record is None:
        # No action turn in the script — overall is the last turn's class.
        result.overall = (
            result.records[-1].verdict.failure_class
            if result.records
            else (FailureClass.EARLY_STOP)
        )
        return result

    if action_record.verdict.passed:
        result.overall = FailureClass.PASS
        return result

    # The first attempt didn't act → nudge once and see if it recovers.
    nudge_record = _run_turn(
        runner,
        sandbox,
        NUDGE,
        model=model,
        agent=agent,
        session=session,
        workspace_root=workspace_root,
        run_turn=turn_fn,
    )
    result.records.append(nudge_record)
    result.session_id = session or nudge_record.turn.session_id

    if nudge_record.verdict.passed:
        result.overall = FailureClass.ONLY_ACTS_ON_NUDGE
    else:
        # Still no side effect after the nudge → fully stalled; keep the more
        # specific class from the first attempt (stall / refusal / hallucinated…).
        result.overall = action_record.verdict.failure_class
    return result
