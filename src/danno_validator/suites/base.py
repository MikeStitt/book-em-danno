"""The benchmark-task abstraction: `BenchTask` + `BenchVerdict` + the run loop.

A `BenchTask` generalises `level2.Level2Task` to externally-sourced benchmark
instances: it seeds an instance into the mounted workspace, exposes the prompt the
agent is given, resets the instance between agent runs, and grades by running the
instance's own tests in the VM. `run_bench_task` drives one task against one
agent-under-test (a `TurnFn`) and composes the result with the shared oracle, so a
benchmark row reads on the same axes as an L2 row (side-effect pass/fail + tool
calls + latency).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from book_em_danno.core.exec import Runner
from danno_validator.driver import Turn, TurnFn, opencode_run
from danno_validator.oracle import TurnVerdict, classify_turn


@runtime_checkable
class BenchTask(Protocol):
    """One benchmark instance, mapped onto the L2 seed/run/grade contract.

    `provision` does the one-time, expensive setup for the instance (clone a repo at
    a base commit, install its deps, seed stub files) — called once per sandbox.
    `reset` restores the instance to its starting state between agent/model variants
    (e.g. `git reset --hard`); for a stub-only task it may be a no-op re-seed.
    `grade` runs the instance's own tests in the VM and returns True iff they pass.
    """

    @property
    def id(self) -> str: ...
    @property
    def prompt(self) -> str: ...
    def provision(self, runner: Runner, sandbox: str, workspace: Path) -> None: ...
    def reset(self, runner: Runner, sandbox: str, workspace: Path) -> None: ...
    def grade(self, runner: Runner, sandbox: str, workspace: Path) -> bool: ...


@dataclass(frozen=True)
class BenchVerdict:
    """The outcome of one (task x agent) benchmark run.

    `passed` is the ground truth (the instance's tests passed); `verdict` is the
    shared oracle's classification of the turn (so a failure reads as stall /
    refusal / error / hallucinated-tool, not just "tests failed"); `tool_calls`,
    `tokens`, `cost`, and `latency_s` mirror the L1/L2 result fields for the report.
    """

    task_id: str
    suite: str
    passed: bool
    verdict: TurnVerdict
    tool_calls: int
    tokens: int
    cost: float
    latency_s: float
    error_summary: str | None = None


def run_bench_task(
    runner: Runner,
    sandbox: str,
    *,
    task: BenchTask,
    suite: str,
    workspace: Path,
    model: str | None = None,
    agent: str | None = None,
    run_turn: TurnFn | None = None,
) -> BenchVerdict:
    """Run one benchmark `task` against one agent in `sandbox`, returning a verdict.

    The instance must already be provisioned (`task.provision`); this resets it,
    runs a single headless turn with the task prompt, grades by the instance's tests,
    and classifies the turn with the shared oracle (`side_effect = tests passed`).
    `run_turn` is the agent-under-test's turn producer (`opencode_run` by default,
    resolved at call time so a monkeypatched `base.opencode_run` still applies).
    """
    turn_fn = run_turn or opencode_run
    task.reset(runner, sandbox, workspace)
    start = time.monotonic()
    turn: Turn = turn_fn(
        runner,
        sandbox,
        task.prompt,
        agent=agent,
        model=model,
        skip_permissions=True,
        workspace=workspace,
    )
    latency = time.monotonic() - start
    passed = task.grade(runner, sandbox, workspace)
    verdict = classify_turn(turn, side_effect=passed, expects_action=True)
    return BenchVerdict(
        task_id=task.id,
        suite=suite,
        passed=passed,
        verdict=verdict,
        tool_calls=turn.tool_call_count,
        tokens=turn.tokens,
        cost=turn.cost,
        latency_s=latency,
        error_summary=turn.error_summary,
    )
