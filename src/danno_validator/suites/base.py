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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from book_em_danno.capture.gate import GateTally
from book_em_danno.capture.proxy import read_captures
from book_em_danno.capture.wiring import CaptureBinding, RunningCaptures
from book_em_danno.commands import sandbox_cli
from book_em_danno.core.exec import GateBreach, Runner, log_warn
from danno_validator import harnesses
from danno_validator.driver import Turn, TurnFn, opencode_run
from danno_validator.oracle import FailureClass, TurnVerdict, classify_turn, gate_verdict
from danno_validator.suites.config import ResolvedGates, watchdog_max_turns
from danno_validator.telemetry.sampler import (
    ResourceSummary,
    SampleBinding,
    read_samples,
    summarize,
)
from danno_validator.telemetry.wire_metrics import (
    TurnWireMetrics,
    metrics_from_files,
    metrics_from_summaries,
    write_metrics,
    write_transcript,
)


@dataclass(frozen=True)
class GradeResult:
    """The outcome of running an instance's tests: `passed` is the ground truth, and
    `report` is the (tail of the) test-runner stdout+stderr — fed back to the agent as
    the failure context on a second attempt, and kept for diagnostics."""

    passed: bool
    report: str = ""


# Cap on the test-runner output kept in a `GradeResult.report` (chars). Enough to carry
# the failing assertions/compile errors into a retry prompt without blowing the context.
_REPORT_TAIL = 6000


def grade_report(stdout: str, stderr: str, *, limit: int = _REPORT_TAIL) -> str:
    """The tail of a test run's combined stdout+stderr, for retry feedback / diagnostics.
    Keeps the END (where pytest/cargo/gradle print the failure summary), not the head."""
    combined = (stdout + ("\n" + stderr if stderr else "")).strip()
    if len(combined) <= limit:
        return combined
    return "…(truncated)…\n" + combined[-limit:]


# Builds the next-attempt prompt from `(original_prompt, grade_report)` — the retry
# leg of a multi-attempt protocol (Aider Polyglot's 2-attempt convention).
RetryPrompt = Callable[[str, str], str]


def default_retry_prompt(original_prompt: str, report: str) -> str:
    """Re-prompt after a failed attempt: the original task plus the test-runner output,
    so the agent iterates on the code it already left on disk (Aider's methodology)."""
    return (
        f"{original_prompt}\n\n"
        "--- Your previous attempt did not pass the tests. The test runner reported: ---\n\n"
        f"{report}\n\n"
        "Fix your solution so every test passes. Do not edit the test files."
    )


@runtime_checkable
class BenchTask(Protocol):
    """One benchmark instance, mapped onto the L2 seed/run/grade contract.

    `provision` does the one-time, expensive setup for the instance (clone a repo at
    a base commit, install its deps, seed stub files) — called once per sandbox.
    `reset` restores the instance to its starting state between agent/model variants
    (e.g. `git reset --hard`); for a stub-only task it may be a no-op re-seed.
    `grade` runs the instance's own tests in the VM and returns a `GradeResult`
    (passed + the test output, so a multi-attempt loop can feed failures back).
    """

    @property
    def id(self) -> str: ...
    @property
    def prompt(self) -> str: ...
    def provision(self, runner: Runner, sandbox: str, workspace: Path) -> None: ...
    def reset(self, runner: Runner, sandbox: str, workspace: Path) -> None: ...
    def grade(self, runner: Runner, sandbox: str, workspace: Path) -> GradeResult: ...


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
    # Runaway-gate observability (only populated when the cell ran under gates):
    rounds: int | None = None  # gate-tally inference rounds — the Gate-1 axis, distinct from
    #   `tool_calls`; excludes grading (the tally stops before `task.grade`). None if ungated.
    gate: GateBreach | None = None  # the breach that killed the cell (gate/observed/limit)
    survivors: tuple[int, ...] | None = None  # harness PIDs alive after the turn; () = clean
    termination: str = "completed"  # "gate_kill" if a gate killed the cell, else "completed"
    #   (orthogonal to `passed`: a killed cell is a gate event regardless of grading)


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


# Harness processes to reap in the sandbox VM after a runaway-gate kill. The host-side
# `sbx exec` kill does NOT propagate into the VM (verified live), so without this a killed
# runaway keeps burning VM CPU there — and, in the shared aider sandbox, bleeds into the
# next cell. The pattern is broad by design: the sandbox is danno-owned and runs one harness
# turn at a time (opencode / claurst, and — once added — codex). It is the union of every
# registered harness's `reap_patterns`, so adding a harness needs no edit here — the registry
# is the single source of truth.
_REAP_PATTERN = "|".join(
    pattern for name in harnesses.all_names() for pattern in harnesses.get(name).reap_patterns
)


def _reap_harness(runner: Runner, sandbox: str) -> None:
    """Best-effort `pkill` of the harness inside `sandbox` after the watchdog killed the
    host-side exec (which does not reap the VM). Never raises (called under suppression)."""
    runner.run(
        [
            *sandbox_cli.base(),
            "exec",
            sandbox,
            "bash",
            "-lc",
            f"pkill -9 -f '{_REAP_PATTERN}' 2>/dev/null; true",
        ],
        why=f"reap harness processes in '{sandbox}' after runaway-gate kill",
        check=False,
    )


# Harness-only survivor probe. The alternatives are bracketed (`[o]pencode`, not `opencode`)
# so `pgrep -f` does not match its OWN `bash -lc` wrapper (whose cmdline carries the pattern),
# which would report a phantom survivor on an idle sandbox. Unlike `_REAP_PATTERN`, this omits
# any persistent in-VM helper (which is never a turn 'survivor'). It is the union of every
# registered harness's `survivor_patterns` (the registry is the single source of truth).
_SURVIVOR_PATTERN = "|".join(
    pattern for name in harnesses.all_names() for pattern in harnesses.get(name).survivor_patterns
)


def _surviving_harness_pids(runner: Runner, sandbox: str) -> tuple[int, ...]:
    """Harness PIDs still alive in `sandbox` after the turn (and any post-kill reap). The
    post-turn invariant is that this is empty: a clean cell's harness exits and a killed one is
    reaped, so a non-empty result means a runaway leaked past the kill and will burn VM CPU
    (and, in a shared sandbox, bleed into the next cell). Best-effort — never raises (a missing
    sandbox CLI or already-gone sandbox reads as no survivors, not an errored row)."""
    try:
        result = runner.capture(
            [
                *sandbox_cli.base(),
                "exec",
                sandbox,
                "bash",
                "-lc",
                f"pgrep -f '{_SURVIVOR_PATTERN}' || true",
            ],
            check=False,
        )
    except OSError:
        return ()
    return tuple(int(p) for p in result.stdout.split() if p.strip().isdigit())


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
    gates: ResolvedGates | None = None,
    attempts: int = 1,
    retry_prompt: RetryPrompt | None = None,
) -> BenchVerdict:
    """Run one benchmark `task` against one agent in `sandbox`, returning a verdict.

    The instance must already be provisioned (`task.provision`); this resets it,
    runs up to `attempts` headless turns with the task prompt (grading between turns
    and re-prompting with the test output on a failure — Aider Polyglot's 2-attempt
    protocol; `attempts=1` is the single-shot default), grades by the instance's
    tests, and classifies the final turn with the shared oracle (`side_effect = tests
    passed`). All attempts run inside one capture/gate/sample window (they are one
    cell): the tally, token budget, and wall-clock span the whole multi-attempt
    session, and grading between turns never dials the capture proxy so it cannot
    inflate the round count.
    `run_turn` is the harness-under-test's turn producer (`opencode_run` by default,
    resolved at call time so a monkeypatched `base.opencode_run` still applies).
    `capture`, when set, records this permutation's wire traffic to its own
    `<suite>/<task>/<model>.<backend>.jsonl` and feeds the runaway-gate tally.
    `sampler`, when set (`danno bench --sample`), profiles host CPU/GPU/mem/VRAM over
    the turn window and its peak/mean rollups (§5.5) land on `BenchVerdict.resource`.
    `gates`, when set, bounds the cell: a live `GateTally` (fed by the capture proxy)
    plus a wall clock are polled by `runner.watching()`, which kills the turn on the
    first breach — recorded as a `runaway`/`over-budget`/`timeout` verdict
    (`.docs/plan-bench-runaway-gates.md`).
    """
    turn_fn = run_turn or opencode_run
    task.reset(runner, sandbox, workspace)
    resource: ResourceSummary | None = None
    wire: TurnWireMetrics | None = None
    tally = GateTally() if gates is not None else None
    start = time.monotonic()
    captures: RunningCaptures | None = None
    with contextlib.ExitStack() as stack:
        if capture is not None:
            # The handle stays valid after the block closes (the servers outlive
            # `serve_forever`), so the metrics-only path can roll up numbers post-turn.
            captures = stack.enter_context(
                capture.permutation(suite=suite, task_id=task.id, model=model, tally=tally)
            )
        if sampler is not None:
            stack.enter_context(sampler.permutation(suite=suite, task_id=task.id, model=model))
        watch = (
            stack.enter_context(
                runner.watching(
                    probe=tally,
                    # Option B: the harness's own --max-turns/agent.steps is set to
                    # gates.max_turns; the external kill sits a grace margin above it so the
                    # harness stops gracefully first (`.docs/plan-bench-runaway-gates.md` §3.2).
                    max_turns=watchdog_max_turns(gates.max_turns),
                    max_tokens=gates.max_tokens,
                    timeout_s=gates.timeout_s,
                    on_kill=lambda: _reap_harness(runner, sandbox),
                )
            )
            if gates is not None
            else None
        )
        make_retry = retry_prompt or default_retry_prompt
        prompt = task.prompt
        turn: Turn | None = None
        grade: GradeResult | None = None
        rounds: int | None = None
        for attempt in range(max(1, attempts)):
            turn = turn_fn(
                runner,
                sandbox,
                prompt,
                agent=agent,
                model=model,
                skip_permissions=True,
                workspace=workspace,
            )
            if watch is not None and watch.breach is not None:
                break  # a gate killed this turn: no retry (ground-truth grade happens post-block)
            # Snapshot the Gate-1 round count NOW — after the turn's inference, before grading.
            # Grading execs the instance's tests in the VM and never dials the capture proxy, so
            # it cannot inflate the tally; snapshotting here keeps that guarantee across attempts
            # (the last turn's snapshot is the cell's true round count).
            if tally is not None:
                rounds = tally.inference_calls()
            grade = task.grade(runner, sandbox, workspace)
            if grade.passed or attempt + 1 >= max(1, attempts):
                break
            prompt = make_retry(task.prompt, grade.report)
    latency = time.monotonic() - start
    assert turn is not None  # the loop runs at least once (attempts >= 1)
    breach = watch.breach if watch is not None else None
    if tally is not None and tally.blind():
        # Fail loud (Working Rule 8): the proxy saw inference POSTs but recognised none as a
        # round, so Gates 1/2 were inert for this cell — an unknown wire dialect. Never let a
        # cell silently run un-gated (F1). `provenance` still records the caps as if in force.
        log_warn(
            f"gate sensor blind for {suite}/{task.id} {model}: proxy saw POST traffic but "
            "counted 0 inference rounds (unrecognised wire dialect) — Gates 1/2 were inert."
        )
    if sampler is not None:
        resource = summarize(
            read_samples(sampler.permutation_path(suite=suite, task_id=task.id, model=model))
        )
    if capture is not None:
        assert captures is not None  # entered together above
        wire = _derive_wire(capture, captures, suite=suite, task_id=task.id, model=model)
    if tally is not None and rounds is None:
        # A gate killed the turn before the in-loop snapshot — read the tally now (grading,
        # which never dials the proxy, hasn't run yet, so this is still grade-free).
        rounds = tally.inference_calls()
    if grade is None:
        # The loop broke before grading (a gate killed the turn). Grade once now for the
        # ground-truth `passed` — the verdict is still a gate event (below), but the row
        # records what the workspace actually achieved.
        grade = task.grade(runner, sandbox, workspace)
    passed = grade.passed
    survivors: tuple[int, ...] | None = None
    if gates is not None:
        # Post-turn invariant: no harness left running (a clean turn exits; a killed one is
        # reaped by on_kill). A survivor means the runaway leaked — fail loud (Working Rule 8).
        survivors = _surviving_harness_pids(runner, sandbox)
        if survivors:
            log_warn(
                f"surviving harness process(es) {list(survivors)} in '{sandbox}' after "
                f"{suite}/{task.id} {model}: a runaway leaked past the kill/reap and will burn "
                "VM CPU (and, in a shared sandbox, bleed into the next cell)."
            )
    if breach is not None:
        # A killed cell is a gate event regardless of what grading finds in the workspace.
        verdict = gate_verdict(breach, tool_call_count=turn.tool_call_count)
        error_summary: str | None = verdict.rationale
    else:
        verdict = classify_turn(turn, side_effect=passed, expects_action=True)
        error_summary = turn.error_summary
    return BenchVerdict(
        task_id=task.id,
        suite=suite,
        passed=passed,
        verdict=verdict,
        tool_calls=turn.tool_call_count,
        tokens=turn.tokens,
        cost=turn.cost,
        latency_s=latency,
        error_summary=error_summary,
        model=model,
        resource=resource,
        wire=wire,
        rounds=rounds,
        gate=breach,
        survivors=survivors,
        termination="gate_kill" if breach is not None else "completed",
    )


def _derive_wire(
    capture: CaptureBinding,
    captures: RunningCaptures,
    *,
    suite: str,
    task_id: str,
    model: str | None,
) -> TurnWireMetrics:
    """Derive this permutation's wire metrics and write its sidecars.

    Save-mode (`persist=True`) parses the just-written capture JSONL, then writes the
    numeric `metrics/` sidecar AND the body-bearing readable `transcripts/` (§1/§2/§6/§3.4).
    Under `--no-save-captures` (`persist=False`) no JSONL exists — the numbers roll up from
    the proxy's in-RAM body-free summaries (byte-identical to the file path, see the parity
    test) and ONLY the numeric `metrics/` sidecar is written; the transcript, which would
    contain message bodies, is deliberately skipped."""
    if capture.persist:
        cap_files = [
            t.capture_file
            for t in capture.permutation_targets(suite=suite, task_id=task_id, model=model)
        ]
        wire = metrics_from_files(cap_files)
        write_metrics(capture.metrics_path(suite=suite, task_id=task_id, model=model), wire)
        records = [rec for f in cap_files for rec in read_captures(f)]
        write_transcript(
            capture.transcript_path(suite=suite, task_id=task_id, model=model), records
        )
        return wire
    wire = metrics_from_summaries(captures.read_summaries())
    write_metrics(capture.metrics_path(suite=suite, task_id=task_id, model=model), wire)
    return wire
