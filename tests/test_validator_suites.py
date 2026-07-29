"""Unit tests for the M4 benchmark-task abstraction (`suites.base`) and config
(`suites.config`). No Docker / no network: the task and turn producer are fakes."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from book_em_danno.core.exec import CaptureResult, Runner
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
    assert v.survivors_unknown is False  # #103: ungated cell is not "unknown", it never probed
    assert v.reap is None  # #103: no gate kill -> no reap attempted


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


def test_run_bench_task_errored_cell_terminates_error(tmp_path: Path) -> None:
    # A harness/transport error (an `error` event on the turn) is a hard failure, not a
    # completion: `termination` must reflect that, never say "completed" for a verdict=error
    # row (issue #104 — else a health check keyed off `termination` waves the failure through).
    task = _FakeTask(_passed=False)
    v = base.run_bench_task(
        Runner(),
        "box",
        task=task,
        suite="aider",
        workspace=tmp_path,
        run_turn=_run_turn_returning(_FakeTurn(errs=[{"type": "error"}])),
    )
    assert str(v.verdict.failure_class) == "error"
    assert v.termination == "error"
    assert v.termination != "completed"  # the invariant this issue locks in


def test_error_verdict_terminates_error() -> None:
    # The could-not-even-run path (deps won't install, repo won't clone) is also an error,
    # so it must not be labeled "completed" either (issue #104 invariant).
    v = base.error_verdict("demo/task", "swebench", "provision failed")
    assert str(v.verdict.failure_class) == "error"
    assert v.termination == "error"


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
    monkeypatch.setattr(
        base, "_surviving_harness_pids", lambda runner, sandbox: base.SurvivorProbe(True, ())
    )
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
    # #89 F5-A: the row carries the effective caps it ran under, verdict-local.
    assert v.resolved_gates == ResolvedGates(max_turns=None, max_tokens=None, timeout_s=0.3)


def test_run_bench_task_records_resolved_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #89 F5-A: the resolved caps a cell ran under are attached to its verdict (so a row is
    # self-describing and survives a partial run); an UNGATED cell carries None.
    monkeypatch.setattr(
        base, "_surviving_harness_pids", lambda runner, sandbox: base.SurvivorProbe(True, ())
    )
    gates = ResolvedGates(max_turns=8, max_tokens=1_000_000, timeout_s=600.0)
    v = base.run_bench_task(
        Runner(),
        "box",
        task=_FakeTask(_passed=True),
        suite="aider",
        workspace=tmp_path,
        model="ollama/x",
        run_turn=_run_turn_returning(_FakeTurn()),
        gates=gates,
    )
    assert v.resolved_gates == gates
    ungated = base.run_bench_task(
        Runner(),
        "box",
        task=_FakeTask(_passed=True),
        suite="aider",
        workspace=tmp_path,
        model="ollama/x",
        run_turn=_run_turn_returning(_FakeTurn()),
    )
    assert ungated.resolved_gates is None


def test_run_bench_task_rounds_snapshot_excludes_grading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `rounds` (the Gate-1 count) must be snapshotted BEFORE grading, so grading — which execs
    # the instance's tests in the VM and never dials the capture proxy — cannot inflate it.
    # Prove the ordering: the turn ticks 3 rounds; grading would (wrongly) tick a 4th.
    from book_em_danno.capture.gate import GateTally

    tally = GateTally()
    monkeypatch.setattr(base, "GateTally", lambda: tally)
    monkeypatch.setattr(
        base, "_surviving_harness_pids", lambda runner, sandbox: base.SurvivorProbe(True, ())
    )

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


# --- #103: reap & survivor probe must make their own failures observable --------------------


class _ReapRunner:
    """Minimal runner exposing only the two capture surfaces `_reap_harness` (`capture_unwatched`)
    and `_surviving_harness_pids` (`capture`) touch — so the failure-visibility paths can be
    driven without a sandbox: a canned `CaptureResult`, or an exception on launch."""

    def __init__(
        self,
        *,
        unwatched: CaptureResult | None = None,
        unwatched_exc: BaseException | None = None,
        watched: CaptureResult | None = None,
        watched_exc: BaseException | None = None,
    ) -> None:
        self._unwatched = unwatched
        self._unwatched_exc = unwatched_exc
        self._watched = watched
        self._watched_exc = watched_exc

    def capture_unwatched(self, cmd, *, check=False):  # type: ignore[no-untyped-def]
        if self._unwatched_exc is not None:
            raise self._unwatched_exc
        assert self._unwatched is not None
        return self._unwatched

    def capture(self, cmd, *, check=False):  # type: ignore[no-untyped-def]
        if self._watched_exc is not None:
            raise self._watched_exc
        assert self._watched is not None
        return self._watched


def test_reap_harness_killed_is_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    # pkill exit 0 = matched+signalled: the reap worked, no warning.
    r = _ReapRunner(unwatched=CaptureResult(["x"], 0, "", ""))
    assert base._reap_harness(r, "box") == "killed"  # type: ignore[arg-type]
    assert "[WARNING]" not in capsys.readouterr().err


def test_reap_harness_no_match_is_quiet(capsys: pytest.CaptureFixture[str]) -> None:
    # pkill exit 1 with no stderr = nothing to kill (harness already exited): expected, quiet.
    r = _ReapRunner(unwatched=CaptureResult(["x"], 1, "", ""))
    assert base._reap_harness(r, "box") == "no-match"  # type: ignore[arg-type]
    assert "[WARNING]" not in capsys.readouterr().err


def test_reap_harness_transport_failure_is_loud_and_tagged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # sbx exec never reached the sandbox (OSError on launch): a failed reap must NOT look clean
    # (#103) — it warns loud and tags the row so a swallowed cleanup is visible in the result.
    r = _ReapRunner(unwatched_exc=FileNotFoundError("sbx not found"))
    tag = base._reap_harness(r, "box")  # type: ignore[arg-type]
    assert tag.startswith("exec-failed:")
    err = capsys.readouterr().err
    assert "[WARNING]" in err and "reap could not run" in err


def test_reap_harness_pkill_error_is_loud_and_tagged(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # exit >=2 (pkill/exec error, distinct from the exit-1 no-match case) is a genuine failure.
    r = _ReapRunner(unwatched=CaptureResult(["x"], 2, "", "pkill: bad usage"))
    tag = base._reap_harness(r, "box")  # type: ignore[arg-type]
    assert tag.startswith("error:")
    err = capsys.readouterr().err
    assert "[WARNING]" in err and "could not confirm" in err


def test_surviving_harness_pids_clean_ran(tmp_path: Path) -> None:
    # pgrep exit 1 = no matches = a genuinely clean sandbox: ran=True, no survivors.
    r = _ReapRunner(watched=CaptureResult(["x"], 1, "", ""))
    probe = base._surviving_harness_pids(r, "box")  # type: ignore[arg-type]
    assert probe.ran is True and probe.pids == ()


def test_surviving_harness_pids_reports_pids(tmp_path: Path) -> None:
    r = _ReapRunner(watched=CaptureResult(["x"], 0, "111\n222\n", ""))
    probe = base._surviving_harness_pids(r, "box")  # type: ignore[arg-type]
    assert probe.ran is True and probe.pids == (111, 222)


def test_surviving_harness_pids_probe_error_is_unknown_not_clean() -> None:
    # pgrep/exec exit >=2 = the probe itself could not run: UNKNOWN, never a silent clean () (#103).
    r = _ReapRunner(watched=CaptureResult(["x"], 2, "", "sbx: no such sandbox"))
    probe = base._surviving_harness_pids(r, "box")  # type: ignore[arg-type]
    assert probe.ran is False and probe.pids == ()


def test_surviving_harness_pids_launch_failure_is_unknown() -> None:
    r = _ReapRunner(watched_exc=FileNotFoundError("sbx"))
    probe = base._surviving_harness_pids(r, "box")  # type: ignore[arg-type]
    assert probe.ran is False


def test_run_bench_task_survivor_probe_failure_records_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # End-to-end (#103): a gate kill whose survivor probe can't run records survivors as UNKNOWN
    # (survivors_unknown=True, survivors=None) — never a silent clean () — and warns loud. The
    # reap outcome tag also lands on the row so a failed cleanup is visible in the result.
    monkeypatch.setattr(
        base, "_surviving_harness_pids", lambda runner, sandbox: base.SurvivorProbe(False, ())
    )
    monkeypatch.setattr(base, "_reap_harness", lambda runner, sandbox: "exec-failed:OSError")
    v = base.run_bench_task(
        Runner(),
        "box",
        task=_FakeTask(_passed=False),
        suite="aider",
        workspace=tmp_path,
        model="ollama/x",
        run_turn=_run_turn_wedged,
        gates=ResolvedGates(max_turns=None, max_tokens=None, timeout_s=0.3),
    )
    assert v.termination == "gate_kill"
    assert v.survivors_unknown is True
    assert v.survivors is None  # UNKNOWN, not a verified-clean ()
    assert v.reap == "exec-failed:OSError"  # the failed reap is recorded on the row
    err = capsys.readouterr().err
    assert "[WARNING]" in err and "survivor probe could not run" in err


# --- #105: a 0-request active backend must fail loud, never grade to a silent pass ------------


def _zero_request_binding(tmp_path: Path, *, backend_name: str = "ollama"):  # type: ignore[no-untyped-def]
    """A real single-backend CaptureBinding on a free port. The proxy stands up but the fake
    turn never dials it, so the tally records 0 POSTs for `backend_name` (the #105 signal)."""
    import socket

    from book_em_danno.capture.wiring import CaptureBinding, CaptureTarget

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        port = sock.getsockname()[1]
    return CaptureBinding(
        targets=(
            CaptureTarget(
                backend_name=backend_name,
                real_base_url="http://h:11434/v1",
                upstream="http://127.0.0.1:1",  # never dialed by the fake turn
                proxy_port=port,
                capture_file=tmp_path / f"{backend_name}.jsonl",
                is_local=True,
            ),
        ),
        capture_dir=tmp_path / "captures",
    )


def test_run_bench_task_zero_request_active_backend_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # #105: the model's own backend saw ZERO inference requests — it was never dialed. Even though
    # grading passed (workspace-only), the cell must NOT be reported as a clean pass: it is marked
    # `no_requests` with an ERROR verdict and a loud warning, distinct from a genuine pass/fail.
    monkeypatch.setattr(
        base, "_surviving_harness_pids", lambda runner, sandbox: base.SurvivorProbe(True, ())
    )
    v = base.run_bench_task(
        Runner(),
        "box",
        task=_FakeTask(_passed=True),  # workspace already satisfies the tests
        suite="aider",
        workspace=tmp_path,
        model="ollama/x",
        run_turn=_run_turn_returning(_FakeTurn()),  # returns without dialing the proxy
        capture=_zero_request_binding(tmp_path),
        gates=ResolvedGates(max_turns=10, max_tokens=None, timeout_s=30.0),
    )
    assert v.termination == "no_requests"  # marked, not "completed"
    assert v.verdict.failure_class is FailureClass.ERROR  # not a clean-pass verdict
    assert v.passed is True  # ground truth preserved, but the row is not a silent green
    err = capsys.readouterr().err
    assert "[WARNING]" in err and "0 inference requests" in err and "ollama" in err


def test_run_bench_task_active_backend_with_traffic_is_not_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The guard fires ONLY on a truly-dark active backend: a cell whose active backend DID see
    # traffic (here pre-seeded, as a real dial would) grades normally — no false no_requests.
    tally = base.GateTally()
    tally.observe_post("ollama")  # the active backend was dialed this cell
    monkeypatch.setattr(base, "GateTally", lambda: tally)
    monkeypatch.setattr(
        base, "_surviving_harness_pids", lambda runner, sandbox: base.SurvivorProbe(True, ())
    )
    v = base.run_bench_task(
        Runner(),
        "box",
        task=_FakeTask(_passed=True),
        suite="aider",
        workspace=tmp_path,
        model="ollama/x",
        run_turn=_run_turn_returning(_FakeTurn()),
        capture=_zero_request_binding(tmp_path),
        gates=ResolvedGates(max_turns=10, max_tokens=None, timeout_s=30.0),
    )
    assert v.termination == "completed"
    assert v.verdict.passed is True


def test_run_bench_task_uncaptured_active_backend_not_falsely_flagged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An uncaptured cloud ref (its backend has no proxy this cell) has NO 0-request sensor, so the
    # guard must not fire — `uncaptured_cloud_refs` warns about those separately. Only the local
    # `ollama` backend is proxied; the active backend `anthropic` is not in the target set.
    monkeypatch.setattr(
        base, "_surviving_harness_pids", lambda runner, sandbox: base.SurvivorProbe(True, ())
    )
    v = base.run_bench_task(
        Runner(),
        "box",
        task=_FakeTask(_passed=True),
        suite="aider",
        workspace=tmp_path,
        model="anthropic/claude-x",  # backend 'anthropic' is not among the captured targets
        run_turn=_run_turn_returning(_FakeTurn()),
        capture=_zero_request_binding(tmp_path, backend_name="ollama"),
        gates=ResolvedGates(max_turns=10, max_tokens=None, timeout_s=30.0),
    )
    assert v.termination == "completed"  # not flagged: no sensor for the active backend
