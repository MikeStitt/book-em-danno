"""V3 — the in-sandbox termination matrix (GV2 for opencode+occ, GV3 adds claurst).

Automates `.docs/live-verify-runaway-gates.md`: provisions a REAL sandbox, points its
harness at the stub AI (via the always-on capture proxy = gate sensor), and drives scripted
turns to pin each gate's kill/graceful-stop behavior — including option B's "graceful
self-stop wins the race" and Gate 2, neither of which PR #88 ever live-verified.

⚠️ NOT YET LIVE-VERIFIED — see `gates_fixtures` module docstring. Every test carries a
`pytest-timeout` ceiling so a regression fails instead of hanging (the plan's rule: the
runaway-protection tests must not themselves hang).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gates_fixtures import (
    LOOP_TOOL,
    LOOP_TOOL_ARGS,
    provisioned_sandbox,
    requires_docker,
    run_scripted_turn,
    scripted_backend,
    surviving_harness_pids,
)

from book_em_danno.core.exec import Runner
from book_em_danno.stubai import Drip, Finish, ToolCall, ToolLoop
from danno_validator.oracle import gate_verdict
from danno_validator.suites.config import ResolvedGates

pytestmark = [pytest.mark.slow, requires_docker]

# GV2 = opencode + occ; claurst is the GV3 row (its local routing needs the relay confirmed).
HARNESSES = ["opencode", "occ", "claurst"]
# Harnesses whose NATIVE turn cap (`--max-turns`) reliably stops the loop before the
# external kill (option B graceful self-stop). opencode's `agent.steps` is advisory at the
# template version, so its runaway is caught by the external kill instead.
GRACEFUL_HARNESSES = {"occ", "claurst"}


@pytest.fixture(scope="module", params=HARNESSES)
def cell(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    harness = request.param
    root = tmp_path_factory.mktemp(f"gates-{harness}")
    name = f"danno-gates-{harness}"
    with provisioned_sandbox(name, harness, root) as workspace:
        yield harness, name, workspace


@pytest.mark.timeout(900)
def test_clean_finish_no_breach(cell, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    harness, name, workspace = cell
    script = [ToolCall(LOOP_TOOL, LOOP_TOOL_ARGS)] * 3 + [Finish("all done")]
    gates = ResolvedGates(max_turns=50, max_tokens=2_000_000, timeout_s=1800.0)
    with scripted_backend(script, tmp_path) as backend:
        turn, watch = run_scripted_turn(
            Runner(apply=True),
            name,
            backend,
            "do the task",
            harness=harness,
            gates=gates,
            workspace=workspace,
        )
        rounds = backend.tally.inference_calls()
    assert watch.breach is None  # a well-behaved cell is never killed
    assert rounds == 4  # 3 tool calls + the final answer
    assert not surviving_harness_pids(name)  # nothing left running


@pytest.mark.timeout(900)
def test_runaway_loop_is_bounded(cell, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    harness, name, workspace = cell
    script = [ToolLoop(LOOP_TOOL, LOOP_TOOL_ARGS, n=None)]  # never stops on its own
    gates = ResolvedGates(max_turns=5, max_tokens=2_000_000, timeout_s=1800.0)
    with scripted_backend(script, tmp_path) as backend:
        _turn, watch = run_scripted_turn(
            Runner(apply=True),
            name,
            backend,
            "loop forever",
            harness=harness,
            gates=gates,
            workspace=workspace,
        )
        rounds = backend.tally.inference_calls()
    if harness in GRACEFUL_HARNESSES:
        # Option B: the native --max-turns stops it cleanly BEFORE the external kill fires.
        assert watch.breach is None
        assert rounds <= 5 + 1  # ~max_turns rounds, no runaway kill
    else:
        # opencode: steps advisory → the external watchdog kills at max_turns + grace.
        assert watch.breach is not None and watch.breach.gate == "runaway"
        assert gate_verdict(watch.breach).failure_class.value == "runaway"
    assert not surviving_harness_pids(name)  # reaped either way


@pytest.mark.timeout(900)
def test_token_budget_gate(cell, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    harness, name, workspace = cell
    script = [ToolLoop(LOOP_TOOL, LOOP_TOOL_ARGS, n=None)]
    # Tiny token cap, generous round/time caps → Gate 2 is the one that must fire. (5 tokens
    # per stub round means a few rounds exceed it.) Gate 2 has no live verification today.
    gates = ResolvedGates(max_turns=1000, max_tokens=20, timeout_s=1800.0)
    with scripted_backend(script, tmp_path) as backend:
        _turn, watch = run_scripted_turn(
            Runner(apply=True),
            name,
            backend,
            "spend tokens",
            harness=harness,
            gates=gates,
            workspace=workspace,
        )
    if harness in GRACEFUL_HARNESSES:
        # A cap-honoring harness may still self-stop on rounds first; if the external gate
        # fires, it must be the token gate, never a round/time gate.
        assert watch.breach is None or watch.breach.gate == "over-budget"
    else:
        assert watch.breach is not None and watch.breach.gate == "over-budget"
    assert not surviving_harness_pids(name)


@pytest.mark.timeout(900)
def test_wallclock_timeout_gate(cell, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    harness, name, workspace = cell
    # A slow drip with huge round/token caps → only Gate 3 (wall clock) can catch it. Re-pins
    # the one row the 2026-07-14 manual run covered.
    script = [Drip("this reply arrives very slowly indeed", tokens_per_s=0.5)]
    gates = ResolvedGates(max_turns=1000, max_tokens=10_000_000, timeout_s=5.0)
    with scripted_backend(script, tmp_path) as backend:
        _turn, watch = run_scripted_turn(
            Runner(apply=True),
            name,
            backend,
            "drip",
            harness=harness,
            gates=gates,
            workspace=workspace,
        )
    assert watch.breach is not None and watch.breach.gate == "timeout"
    assert not surviving_harness_pids(name)


@pytest.mark.timeout(1200)
def test_next_cell_runs_clean_after_kill(cell, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    # Post-kill invariant: a killed runaway leaves no bleed-through — the SAME sandbox runs a
    # normal cell cleanly afterwards.
    harness, name, workspace = cell
    runner = Runner(apply=True)
    with scripted_backend([ToolLoop(LOOP_TOOL, LOOP_TOOL_ARGS, n=None)], tmp_path) as backend:
        run_scripted_turn(
            runner,
            name,
            backend,
            "loop",
            harness=harness,
            gates=ResolvedGates(max_turns=3, max_tokens=2_000_000, timeout_s=60.0),
            workspace=workspace,
        )
    assert not surviving_harness_pids(name)
    with scripted_backend([Finish("clean")], (tmp_path / "second")) as backend2:
        (tmp_path / "second").mkdir(exist_ok=True)
        turn, watch = run_scripted_turn(
            runner,
            name,
            backend2,
            "answer",
            harness=harness,
            gates=ResolvedGates(max_turns=50, max_tokens=2_000_000, timeout_s=1800.0),
            workspace=workspace,
        )
        rounds = backend2.tally.inference_calls()
    assert watch.breach is None and rounds == 1  # the next cell is unaffected
