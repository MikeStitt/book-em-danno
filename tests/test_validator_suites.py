"""Unit tests for the M4 benchmark-task abstraction (`suites.base`) and config
(`suites.config`). No Docker / no network: the task and turn producer are fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from book_em_danno.core.exec import Runner
from danno_validator.suites import base
from danno_validator.suites.config import BenchmarksConfig, load_benchmarks


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

    def grade(self, runner: Runner, sandbox: str, workspace: Path) -> bool:
        self.calls.append("grade")
        return self._passed


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


def test_load_benchmarks_parses_agents_list(tmp_path: Path) -> None:
    p = tmp_path / "benchmarks.toml"
    p.write_text("agents = ['occ', 'claurst', 'claude']\n[swebench]\nenabled = true\n")
    cfg = load_benchmarks(p)
    assert cfg.agents == ["occ", "claurst", "claude"]


def test_load_benchmarks_default_agents_is_empty(tmp_path: Path) -> None:
    # Empty (the default) means the single opencode default — resolved by the bench CLI.
    assert load_benchmarks(tmp_path / "nope.toml").agents == []


def test_load_benchmarks_unknown_agent_fails_loud(tmp_path: Path) -> None:
    p = tmp_path / "benchmarks.toml"
    p.write_text("agents = ['occ', 'gpt5']\n")
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
