"""The benchmark-task abstraction: `BenchTask` + `BenchVerdict` + the run loop.

A `BenchTask` generalises `level2.Level2Task` to externally-sourced benchmark
instances: it seeds an instance into the mounted workspace, exposes the prompt the
agent is given, resets the instance between agent runs, and grades by running the
instance's own tests in the VM. `run_bench_task` drives one task against one
harness-under-test (a `TurnFn`) and composes the result with the shared oracle, so a
benchmark row reads on the same axes as an L2 row (side-effect pass/fail + tool
calls + latency).
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from book_em_danno.capture.proxy import read_captures
from book_em_danno.capture.wiring import CaptureBinding
from book_em_danno.core.exec import Runner
from danno_validator.driver import Turn, TurnFn, opencode_run
from danno_validator.oracle import FailureClass, TurnVerdict, classify_turn
from danno_validator.telemetry.sampler import (
    ResourceSummary,
    SampleBinding,
    read_samples,
    summarize,
)
from danno_validator.telemetry.wire_metrics import (
    TurnWireMetrics,
    metrics_from_files,
    write_metrics,
    write_transcript,
)


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
    model: str | None = None  # the model ref this row ran (the permutation axis)
    resource: ResourceSummary | None = None  # §5 peak/mean rollups (with --sample)
    wire: TurnWireMetrics | None = None  # §1/§2/§6 derived wire metrics (with --capture)


def error_verdict(task_id: str, suite: str, detail: str) -> BenchVerdict:
    """A failed `BenchVerdict` for a task that could not even run (e.g. its deps
    would not install, or its repo would not clone) — so one bad instance shows as
    an errored row instead of aborting the whole run (the suite's fail-loud-per-row
    behavior)."""
    return BenchVerdict(
        task_id=task_id,
        suite=suite,
        passed=False,
        verdict=TurnVerdict(
            failure_class=FailureClass.ERROR,
            promised_action=False,
            tool_call_count=0,
            side_effect=False,
            rationale=detail,
        ),
        tool_calls=0,
        tokens=0,
        cost=0.0,
        latency_s=0.0,
        error_summary=detail,
    )


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
    capture: CaptureBinding | None = None,
    sampler: SampleBinding | None = None,
) -> BenchVerdict:
    """Run one benchmark `task` against one agent in `sandbox`, returning a verdict.

    The instance must already be provisioned (`task.provision`); this resets it,
    runs a single headless turn with the task prompt, grades by the instance's tests,
    and classifies the turn with the shared oracle (`side_effect = tests passed`).
    `run_turn` is the harness-under-test's turn producer (`opencode_run` by default,
    resolved at call time so a monkeypatched `base.opencode_run` still applies).
    `capture`, when set (`danno bench --capture`), records this permutation's wire
    traffic to its own `<suite>/<task>/<model>.<backend>.jsonl` for the turn's duration.
    `sampler`, when set (`danno bench --sample`), profiles host CPU/GPU/mem/VRAM over
    the turn window and its peak/mean rollups (§5.5) land on `BenchVerdict.resource`.
    """
    turn_fn = run_turn or opencode_run
    task.reset(runner, sandbox, workspace)
    resource: ResourceSummary | None = None
    wire: TurnWireMetrics | None = None
    start = time.monotonic()
    with contextlib.ExitStack() as stack:
        if capture is not None:
            stack.enter_context(capture.permutation(suite=suite, task_id=task.id, model=model))
        if sampler is not None:
            stack.enter_context(sampler.permutation(suite=suite, task_id=task.id, model=model))
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
    if sampler is not None:
        resource = summarize(
            read_samples(sampler.permutation_path(suite=suite, task_id=task.id, model=model))
        )
    if capture is not None:
        wire = _derive_wire(capture, suite=suite, task_id=task.id, model=model)
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
        model=model,
        resource=resource,
        wire=wire,
    )


def _derive_wire(
    capture: CaptureBinding, *, suite: str, task_id: str, model: str | None
) -> TurnWireMetrics:
    """Parse this permutation's just-written capture JSONL into derived wire metrics and
    write the `metrics/` + `transcripts/` sidecars (§1/§2/§6/§3.4)."""
    cap_files = [
        t.capture_file
        for t in capture.permutation_targets(suite=suite, task_id=task_id, model=model)
    ]
    wire = metrics_from_files(cap_files)
    write_metrics(capture.metrics_path(suite=suite, task_id=task_id, model=model), wire)
    records = [rec for f in cap_files for rec in read_captures(f)]
    write_transcript(capture.transcript_path(suite=suite, task_id=task_id, model=model), records)
    return wire
