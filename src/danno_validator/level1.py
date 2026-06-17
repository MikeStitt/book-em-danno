"""Level-1 tool/bash battery: a single-shot task with a deterministic oracle.

Where Level 0 asks *does the agent act at all* (the promised-but-didn't-act
stall), Level 1 asks *can it use tools/bash to produce a specific, verifiable
result*. A task is fully declarative — a set of seed files, a prompt, and the
exact output file/content the agent must produce — so the oracle is **objective
and needs no LLM judge** (the plan's L1 contract): the side effect either matches
or it doesn't. That boolean is fed into the *same* pure `classify_turn` the stall
oracle uses, so an L1 failure lands in the existing `FailureClass` taxonomy
(`malformed-tool-args` when a tool errored, `stall`/`hallucinated-tool` when it
talked but never acted, `early-stop`, …) — no L1-only class is needed.

`run_level1` performs I/O (seeds the workspace, drives the sandbox via the
injected `Runner`, re-reads the workspace); the judgement is the pure oracle's, so
the decision logic stays unit-testable. Seeding is **surgical** — it writes the
task's inputs and removes only its own expected-output file, so a leftover from a
prior run can't fake a pass and the seed is safe against any workspace (it never
runs a destructive git reset).

Default = one curated task (`DEFAULT_TASK`); a larger bank / `--full` is a later
milestone, as is the general benchmark-adapter path (Terminal-Bench, InterCode).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from book_em_danno.core.exec import Runner
from danno_validator.driver import Turn, TurnFn, opencode_run
from danno_validator.level0 import DEFAULT_AGENT
from danno_validator.oracle import FailureClass, TurnVerdict, classify_turn


@dataclass(frozen=True)
class Level1Task:
    """A declarative tool/bash task with a deterministic file-output oracle.

    `inputs` are `(filename, content)` pairs seeded into the workspace before the
    run; `output_file` is the file the agent must produce, and `expected_output`
    is its required content (compared stripped). Keeping the task declarative
    means the oracle is a pure file comparison — exactly the L1 "no LLM judge"
    contract — and makes the task itself trivial to unit-test.
    """

    label: str
    prompt: str
    inputs: tuple[tuple[str, str], ...]
    output_file: str
    expected_output: str

    def seed(self, workspace_root: Path) -> None:
        """Write the task's input files and clear its expected output.

        Surgical (Working Rule 3/6): touches only this task's own files, so it
        never needs a destructive git reset and is safe on a borrowed workspace.
        Removing a stale `output_file` first means a leftover from a prior run
        can't make a non-acting model look like a pass (cf. L0's probe reset).
        """
        for name, content in self.inputs:
            (workspace_root / name).write_text(content)
        (workspace_root / self.output_file).unlink(missing_ok=True)

    def check(self, workspace_root: Path) -> bool:
        """True iff `output_file` exists with the expected content (host-side).

        The sandbox mounts the workspace, so the agent's writes show up here.
        """
        f = workspace_root / self.output_file
        try:
            return f.is_file() and f.read_text().strip() == self.expected_output
        except OSError:
            return False


# The curated default: a bash line-count task. Producing the right number
# genuinely requires using a tool (count the seeded file, write the result) — a
# pure "echo this literal string" task wouldn't exercise tool/bash use — yet the
# answer is a single deterministic integer the oracle can verify exactly.
_DATA_LINES: tuple[str, ...] = ("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf")

DEFAULT_TASK = Level1Task(
    label="line-count",
    prompt=(
        "Using a shell command, count how many lines are in the file `data.txt` in "
        "the current project directory. Then write just that number — digits only, "
        "with no other text — to a new file named `line_count.txt` in the same "
        "directory. Use your tools to do it now."
    ),
    inputs=(("data.txt", "\n".join(_DATA_LINES) + "\n"),),
    output_file="line_count.txt",
    expected_output=str(len(_DATA_LINES)),
)


@dataclass
class TaskResult:
    """The Level-1 outcome for one config: the captured turn and its verdict."""

    model: str
    sandbox: str
    workspace_root: Path
    task_label: str
    session_id: str | None
    turn: Turn
    verdict: TurnVerdict
    latency_s: float = field(default=0.0)

    @property
    def passed(self) -> bool:
        return self.verdict.passed

    @property
    def overall(self) -> FailureClass:
        return self.verdict.failure_class

    @property
    def tokens(self) -> int:
        return self.turn.tokens

    @property
    def cost(self) -> float:
        return self.turn.cost


def run_level1(
    runner: Runner,
    sandbox: str,
    *,
    model: str,
    workspace_root: Path,
    task: Level1Task = DEFAULT_TASK,
    agent: str = DEFAULT_AGENT,
    run_turn: TurnFn | None = None,
) -> TaskResult:
    """Run one Level-1 task against `model` in `sandbox`, returning the result.

    Single-shot: the task is seeded, one headless turn is driven, the deterministic
    oracle (`task.check`) decides whether the workspace changed as required, and the
    pure `classify_turn` tags the verdict. Runs `--agent build` (the default `run`
    agent is read-only) in the workspace root (`-w`) so opencode finds the configured
    models and writes land where the oracle looks.

    `run_turn` is the turn producer — `opencode_run` by default (resolved at call
    time so a monkeypatched `level1.opencode_run` still applies); the Claude
    baseline passes `driver.claude_run`.
    """
    turn_fn = run_turn or opencode_run
    task.seed(workspace_root)
    start = time.monotonic()
    turn = turn_fn(
        runner,
        sandbox,
        task.prompt,
        agent=agent,
        model=model,
        skip_permissions=True,
        workspace=workspace_root,
    )
    latency = time.monotonic() - start
    side_effect = task.check(workspace_root)
    verdict = classify_turn(turn, side_effect=side_effect, expects_action=True)
    return TaskResult(
        model=model,
        sandbox=sandbox,
        workspace_root=workspace_root,
        task_label=task.label,
        session_id=turn.session_id,
        turn=turn,
        verdict=verdict,
        latency_s=latency,
    )
