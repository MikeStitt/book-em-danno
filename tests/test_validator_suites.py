"""Unit tests for the M4 benchmark-task abstraction (`suites.base`) and config
(`suites.config`). No Docker / no network: the task and turn producer are fakes."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from book_em_danno.core.exec import Runner
from danno_validator.oracle import FailureClass
from danno_validator.suites import base
from danno_validator.suites.config import (
    BenchmarksConfig,
    GateLimits,
    GatesConfig,
    ResolvedGates,
    load_benchmarks,
    resolve_gates,
    watchdog_max_turns,
)


@dataclass
class _FakeTurn:
    text: str = "done"
    n_tools: int = 1
    errs: list[dict] = field(default_factory=list)

    @property
    def assistant_text(self) -> str:
        return self.text

    @property
    def tool_calls(self) -> list[dict]:
        return [{"tool": "Write", "state": {"status": "completed"}}] * self.n_tools

    @property
    def tool_call_count(self) -> int:
        return self.n_tools

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def tokens(self) -> int:
        return 42

    @property
    def cost(self) -> float:
        return 0.0

    @property
    def errors(self) -> list[dict]:
        return self.errs

    @property
    def error_summary(self) -> str | None:
        return None


@dataclass
class _FakeTask:
    _passed: bool
    calls: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        return "demo/task-1"

    @property
    def prompt(self) -> str:
        return "fix the bug"

    def provision(self, runner: Runner, sandbox: str, workspace: Path) -> None:
        self.calls.append("provision")

    def reset(self, runner: Runner, sandbox: str, workspace: Path) -> None:
        self.calls.append("reset")

    def grade(self, runner: Runner, sandbox: str, workspace: Path) -> base.GradeResult:
        self.calls.append("grade")
        return base.GradeResult(passed=self._passed)


def _run_turn_returning(turn: _FakeTurn) -> base.TurnFn:
    def run(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        return turn

    return run


def test_run_bench_task_pass(tmp_path: Path) -> None:
    task = _FakeTask(_passed=True)
    v = base.run_bench_task(
        Runner(),
        "box",
        task=task,
        suite="aider",
        workspace=tmp_path,
        model="ollama/x",
        run_turn=_run_turn_returning(_FakeTurn()),
    )
    assert v.task_id == "demo/task-1"
    assert v.suite == "aider"
    assert v.passed is True
    assert v.verdict.passed is True  # oracle agrees (side effect verified)
    assert v.tool_calls == 1
    assert v.tokens == 42
    assert task.calls == ["reset", "grade"]  # reset before the turn, grade after
    # Ungated cell: the gate-observability fields stay at their "no gates" defaults.
    assert v.termination == "completed"
    assert v.gate is None
    assert v.rounds is None
    assert v.survivors is None


def test_run_bench_task_fail_classifies_turn(tmp_path: Path) -> None:
    # Tests failed AND the agent made no tool call -> oracle gives a non-pass verdict.
    task = _FakeTask(_passed=False)
    v = base.run_bench_task(
        Runner(),
        "box",
        task=task,
        suite="swebench",
        workspace=tmp_path,
        run_turn=_run_turn_returning(_FakeTurn(text="I will fix it.", n_tools=0)),
    )
    assert v.passed is False
    assert v.verdict.passed is False


def test_load_benchmarks_missing_file_is_all_disabled(tmp_path: Path) -> None:
    cfg = load_benchmarks(tmp_path / "nope.toml")
    assert isinstance(cfg, BenchmarksConfig)
    assert cfg.any_enabled() is False


def test_load_benchmarks_parses_both_suites(tmp_path: Path) -> None:
    p = tmp_path / "benchmarks.toml"
    p.write_text(
        "[aider_polyglot]\nenabled = true\nselect = ['python/anagram', 'go/grep']\n"
        "[swebench]\nenabled = true\nselect = ['django__django-11099']\n"
    )
    cfg = load_benchmarks(p)
    assert cfg.any_enabled() is True
    assert cfg.aider_polyglot.enabled and cfg.aider_polyglot.select == ["python/anagram", "go/grep"]
    assert cfg.swebench.select == ["django__django-11099"]
    assert cfg.swebench.deps == "offline-wheel-cache"  # default


def test_load_benchmarks_parses_harnesses_list(tmp_path: Path) -> None:
    p = tmp_path / "benchmarks.toml"
    p.write_text("harnesses = ['opencode', 'claurst', 'claude']\n[swebench]\nenabled = true\n")
    cfg = load_benchmarks(p)
    assert cfg.harnesses == ["opencode", "claurst", "claude"]


def test_load_benchmarks_default_harnesses_is_empty(tmp_path: Path) -> None:
    # Empty (the default) means the single opencode default — resolved by the bench CLI.
    assert load_benchmarks(tmp_path / "nope.toml").harnesses == []


def test_load_benchmarks_unknown_harness_fails_loud(tmp_path: Path) -> None:
    p = tmp_path / "benchmarks.toml"
    p.write_text("harnesses = ['opencode', 'gpt5']\n")
    with pytest.raises(ValueError, match="invalid benchmarks config"):
        load_benchmarks(p)


def test_load_benchmarks_unknown_key_fails_loud(tmp_path: Path) -> None:
    p = tmp_path / "benchmarks.toml"
    p.write_text("[swebench]\nenabled = true\nbogus = 1\n")
    with pytest.raises(ValueError, match="invalid benchmarks config"):
        load_benchmarks(p)


def test_load_benchmarks_bad_toml_fails_loud(tmp_path: Path) -> None:
    p = tmp_path / "benchmarks.toml"
    p.write_text("[swebench\nenabled = true\n")
    with pytest.raises(ValueError, match="invalid TOML"):
        load_benchmarks(p)


# --- runaway gates (M0) -------------------------------------------------------


def test_gates_default_values_are_backstops() -> None:
    g = BenchmarksConfig().gates
    assert (g.max_turns, g.max_tokens, g.timeout_s) == (50, 2_000_000, 1800.0)
    assert g.harness == {} and g.model == {}


def test_gates_resolution_falls_through_to_global_defaults() -> None:
    r = resolve_gates(GatesConfig(), harness="opencode", model="ollama/x")
    assert (r.max_turns, r.max_tokens, r.timeout_s) == (50, 2_000_000, 1800.0)


def test_gates_resolution_precedence_is_per_field_model_over_harness_over_global() -> None:
    gates = GatesConfig(
        max_turns=50,
        max_tokens=2_000_000,
        timeout_s=1800.0,
        harness={"opencode": GateLimits(max_turns=40)},
        model={"o4-mini": GateLimits(max_turns=80)},
    )
    # opencode + a model with an override: model max_turns wins; max_tokens/timeout_s
    # fall through to the global floor (neither the model nor harness layer set them).
    r = resolve_gates(gates, harness="opencode", model="o4-mini")
    assert r.max_turns == 80  # model layer
    assert r.max_tokens == 2_000_000  # global
    assert r.timeout_s == 1800.0  # global
    # opencode + a model with no override: harness max_turns wins.
    r2 = resolve_gates(gates, harness="opencode", model="qwen")
    assert r2.max_turns == 40  # harness layer
    # a different harness + no model override: global floor.
    r3 = resolve_gates(gates, harness="claurst", model="qwen")
    assert r3.max_turns == 50  # global


def test_gates_resolution_none_disables_a_gate() -> None:
    r = resolve_gates(GatesConfig(timeout_s=None), harness="claurst", model=None)
    assert r.timeout_s is None  # disabled → watchdog skips it
    assert r.max_turns == 50


def test_watchdog_max_turns_adds_grace_above_the_native_cap() -> None:
    # Option B: the harness's own cap = max_turns (graceful); the external kill sits a
    # grace margin above it (max(3, 10%)) so the harness stops first.
    assert watchdog_max_turns(None) is None  # Gate 1 disabled stays disabled
    assert watchdog_max_turns(50) == 55  # +max(3, 5)
    assert watchdog_max_turns(100) == 110  # +max(3, 10)
    assert watchdog_max_turns(10) == 13  # +max(3, 1) → +3 floor


def test_gates_load_from_toml_with_overrides(tmp_path: Path) -> None:
    p = tmp_path / "benchmarks.toml"
    p.write_text(
        "[gates]\nmax_turns = 60\nmax_tokens = 1_000_000\n"
        "[gates.harness.opencode]\nmax_turns = 40\n"
        '[gates.model."o4-mini"]\nmax_turns = 80\n'
    )
    gates = load_benchmarks(p).gates
    assert gates.max_turns == 60 and gates.max_tokens == 1_000_000
    assert gates.timeout_s == 1800.0  # unset → default floor
    assert gates.harness["opencode"].max_turns == 40
    assert gates.model["o4-mini"].max_turns == 80


def test_gates_unknown_key_fails_loud(tmp_path: Path) -> None:
    p = tmp_path / "benchmarks.toml"
    p.write_text("[gates]\nmax_turns = 60\ncost_usd = 2.0\n")  # cost tier was removed
    with pytest.raises(ValueError, match="invalid benchmarks config"):
        load_benchmarks(p)


def _run_turn_wedged(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
    # A harness turn that never returns — the Gate 3 wall clock must kill it.
    runner.capture([sys.executable, "-c", "import time; time.sleep(30)"])
    return _FakeTurn()


def test_run_bench_task_gate_timeout_kills_and_classifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # End-to-end through run_bench_task (real subprocess, no Docker): a wedged turn under
    # a 0.3s Gate 3 is killed and recorded as a `timeout` verdict, not a normal stall/pass.
    monkeypatch.setattr(base, "_surviving_harness_pids", lambda runner, sandbox: ())
    task = _FakeTask(_passed=False)
    v = base.run_bench_task(
        Runner(),
        "box",
        task=task,
        suite="aider",
        workspace=tmp_path,
        model="ollama/x",
        run_turn=_run_turn_wedged,
        gates=ResolvedGates(max_turns=None, max_tokens=None, timeout_s=0.3),
    )
    assert v.verdict.failure_class is FailureClass.TIMEOUT
    assert v.passed is False
    assert "timeout" in (v.error_summary or "")
    assert task.calls == ["reset", "grade"]  # still graded after the kill
    # A killed cell records the full breach + a gate_kill termination, distinct from `passed`.
    assert v.termination == "gate_kill"
    assert v.gate is not None and v.gate.gate == "timeout"
    assert v.gate.limit == 0.3
    assert v.rounds == 0  # gated but no inference round reached the (absent) proxy
    assert v.survivors == ()  # probe stubbed: no leaked harness


def test_run_bench_task_rounds_snapshot_excludes_grading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `rounds` (the Gate-1 count) must be snapshotted BEFORE grading, so grading — which execs
    # the instance's tests in the VM and never dials the capture proxy — cannot inflate it.
    # Prove the ordering: the turn ticks 3 rounds; grading would (wrongly) tick a 4th.
    from book_em_danno.capture.gate import GateTally

    tally = GateTally()
    monkeypatch.setattr(base, "GateTally", lambda: tally)
    monkeypatch.setattr(base, "_surviving_harness_pids", lambda runner, sandbox: ())

    def _turn(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        for _ in range(3):
            tally.record(tokens=5)  # three inference rounds during the turn
        return _FakeTurn()

    class _GradeBumpsTally(_FakeTask):
        def grade(self, runner: Runner, sandbox: str, workspace: Path) -> base.GradeResult:
            tally.record(tokens=5)  # grading must NOT be counted as a round
            return super().grade(runner, sandbox, workspace)

    v = base.run_bench_task(
        Runner(),
        "box",
        task=_GradeBumpsTally(_passed=True),
        suite="aider",
        workspace=tmp_path,
        model="ollama/x",
        run_turn=_turn,
        gates=ResolvedGates(max_turns=10, max_tokens=None, timeout_s=30.0),
    )
    assert v.rounds == 3  # snapshotted before grade bumped the tally to 4
    assert v.tool_calls == 1  # rounds (3) is a distinct axis from tool_calls (1)
    assert v.termination == "completed" and v.gate is None
    assert v.survivors == ()
