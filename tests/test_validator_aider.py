"""Unit tests for the Aider Polyglot suite — `load_aider_task` parsing of the Exercism
layout, the whole-dir seeding + test-file integrity restore, the language toolchain
helpers, and the 2-attempt run loop. No Docker / no network: the exercise tree is
fabricated in tmp_path and the in-VM `capture_exec` is stubbed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from book_em_danno.core.exec import CaptureResult, Runner
from danno_validator.suites import aider, run
from danno_validator.suites.aider import (
    languages_in,
    load_aider_task,
    toolchain_egress,
)
from danno_validator.suites.base import GradeResult, run_bench_task


def _make_exercise(
    root: Path,
    *,
    lang: str = "python",
    slug: str = "anagram",
    instructions: str = "Find the anagrams.",
) -> Path:
    base = root / lang / "exercises" / "practice" / slug
    (base / ".meta").mkdir(parents=True)
    (base / ".docs").mkdir(parents=True)
    (base / ".meta" / "config.json").write_text(
        json.dumps(
            {"files": {"solution": [f"{slug}.py"], "test": [f"{slug}_test.py"], "example": []}}
        )
    )
    # The reference solution lives under .meta — it must NOT be seeded into the workspace.
    (base / ".meta" / "example.py").write_text("SECRET_REFERENCE = True\n")
    (base / f"{slug}.py").write_text("def solve():\n    pass\n")
    (base / f"{slug}_test.py").write_text("def test_it():\n    assert solve()\n")
    (base / ".docs" / "instructions.md").write_text(instructions)
    return base


def test_load_aider_task_parses_exercism_layout(tmp_path: Path) -> None:
    _make_exercise(tmp_path)
    task = load_aider_task(tmp_path, "python/anagram")
    assert task.id == "python/anagram"
    assert task.language == "python"
    assert task.subdir == "python/anagram"  # language-namespaced (no cross-language collision)
    assert [p for p, _ in task.solution_files] == ["anagram.py"]
    assert list(task.test_files) == ["anagram_test.py"]
    assert [p for p, _ in task.protected_files] == ["anagram_test.py"]
    assert "Find the anagrams." in task.prompt
    assert "anagram.py" in task.prompt
    assert "Do not edit the test" in task.prompt


def test_load_aider_task_unknown_language_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported language"):
        load_aider_task(tmp_path, "cobol/widget")


def test_load_aider_task_bad_id_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be"):
        load_aider_task(tmp_path, "no-slash")


def test_load_aider_task_missing_exercise_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        load_aider_task(tmp_path, "python/does-not-exist")


def test_provision_seeds_whole_dir_but_not_reference_solution(tmp_path: Path) -> None:
    checkout = tmp_path / "polyglot"
    base = _make_exercise(checkout)
    # A build-scaffolding file outside solution/test must still be seeded.
    (base / "conftest.py").write_text("# scaffolding\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    task = load_aider_task(checkout, "python/anagram")
    task.provision(Runner(), "box", ws)
    d = ws / "python" / "anagram"
    assert (d / "anagram.py").is_file()
    assert (d / "anagram_test.py").is_file()
    assert (d / "conftest.py").is_file()  # scaffolding seeded
    assert not (d / ".meta").exists()  # reference solution NOT leaked
    assert not (d / ".docs").exists()


def test_grade_restores_tampered_test_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    checkout = tmp_path / "polyglot"
    _make_exercise(checkout)
    ws = tmp_path / "ws"
    ws.mkdir()
    task = load_aider_task(checkout, "python/anagram")
    task.provision(Runner(), "box", ws)
    test_path = ws / "python" / "anagram" / "anagram_test.py"
    test_path.write_text("def test_it():\n    assert True  # agent nerfed the test\n")
    graded: list[str] = []

    def fake(runner, name, command, *, check=False):  # type: ignore[no-untyped-def]
        graded.append(command)
        return CaptureResult([command], 0, "1 passed", "")

    monkeypatch.setattr(aider, "capture_exec", fake)
    result = task.grade(Runner(), "box", ws)
    assert isinstance(result, GradeResult)
    # The canonical test was restored before grading — the agent's edit is gone.
    assert "assert solve()" in test_path.read_text()
    assert graded and "pytest" in graded[0]


def test_languages_in_and_toolchain_egress() -> None:
    select = ["python/anagram", "go/wordy", "python/wordy", "rust/luhn-from"]
    assert languages_in(select) == ["python", "go", "rust"]  # de-duped, first-seen order
    egress = toolchain_egress(["go", "rust"])
    assert "go.dev" in egress
    assert "sh.rustup.rs" in egress
    # No duplicates.
    assert len(egress) == len(set(egress))


def test_install_toolchains_is_stamp_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(runner, name, command, *, check=False):  # type: ignore[no-untyped-def]
        seen.append(command)
        return CaptureResult([command], 0, "", "")

    monkeypatch.setattr(aider, "capture_exec", fake)
    aider.install_toolchains(Runner(), "box", ["python", "javascript"])
    # python has an install command (guarded by a stamp); javascript's install is empty.
    assert len(seen) == 1
    assert "pytest" in seen[0]
    assert "danno-aider-lang-python.ok" in seen[0]


def _stub_capture(monkeypatch: pytest.MonkeyPatch, *, grade_ok: bool) -> list[str]:
    """Stub the in-VM `capture_exec` used by AiderTask.grade + install_toolchains."""
    seen: list[str] = []

    def fake(runner, name, command, *, check=False):  # type: ignore[no-untyped-def]
        seen.append(command)
        rc = 0 if (grade_ok or "pytest" not in command) else 1
        return CaptureResult([command], rc, "", "boom" if rc else "")

    monkeypatch.setattr(aider, "capture_exec", fake)
    return seen


def test_run_aider_suite_seeds_runs_and_grades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "polyglot"
    _make_exercise(checkout)
    ws = tmp_path / "ws"
    ws.mkdir()
    commands = _stub_capture(monkeypatch, grade_ok=True)
    turns: list[dict] = []

    def fake_run_turn(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        turns.append(kw)
        return _FakeTurn()

    verdicts = run.run_aider_suite(
        Runner(),
        "box",
        checkout=checkout,
        select=["python/anagram"],
        workspace=ws,
        run_turn=fake_run_turn,
        model="ollama/x",
    )
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.task_id == "python/anagram" and v.suite == "aider"
    assert v.passed is True
    # The stub solution + test were seeded into the language-namespaced subdir.
    assert (ws / "python" / "anagram" / "anagram.py").is_file()
    assert (ws / "python" / "anagram" / "anagram_test.py").is_file()
    # The turn's cwd was bound to the exercise subdir.
    assert turns[0]["workspace"] == ws / "python" / "anagram"
    # Passing on the first attempt means exactly one turn (no retry).
    assert len(turns) == 1
    assert any("pytest" in c for c in commands)


def test_run_aider_suite_retries_once_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "polyglot"
    _make_exercise(checkout)
    ws = tmp_path / "ws"
    ws.mkdir()
    _stub_capture(monkeypatch, grade_ok=False)
    prompts: list[str] = []

    def fake_run_turn(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        prompts.append(prompt)
        return _FakeTurn(n_tools=0)

    verdicts = run.run_aider_suite(
        Runner(),
        "box",
        checkout=checkout,
        select=["python/anagram"],
        workspace=ws,
        run_turn=fake_run_turn,
        model=None,
    )
    assert verdicts[0].passed is False
    # 2-attempt protocol: two turns, and the retry carries the test output back.
    assert len(prompts) == 2
    assert "did not pass" in prompts[1]
    assert "boom" in prompts[1]


def test_run_bench_task_single_attempt_does_not_retry() -> None:
    task = _FailingTask()
    turns: list[str] = []

    def fake_run_turn(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        turns.append(prompt)
        return _FakeTurn(n_tools=0)

    verdict = run_bench_task(
        Runner(),
        "box",
        task=task,
        suite="aider",
        workspace=Path("/tmp/ws"),
        run_turn=fake_run_turn,
        attempts=1,
    )
    assert verdict.passed is False
    assert len(turns) == 1  # attempts=1 never retries even on failure


class _FailingTask:
    """A minimal `BenchTask` whose grade always fails, for the run-loop tests."""

    id = "fake/task"
    prompt = "do the thing"

    def provision(self, runner, sandbox, workspace):  # type: ignore[no-untyped-def]
        pass

    def reset(self, runner, sandbox, workspace):  # type: ignore[no-untyped-def]
        pass

    def grade(self, runner, sandbox, workspace):  # type: ignore[no-untyped-def]
        return GradeResult(passed=False, report="still failing")


class _FakeTurn:
    def __init__(self, n_tools: int = 3) -> None:
        self._n = n_tools

    assistant_text = "done"
    session_id = None
    tokens = 10
    cost = 0.0
    errors: list[dict] = []
    error_summary = None

    @property
    def tool_calls(self) -> list[dict]:
        return [{"tool": "Write", "state": {"status": "completed"}}] * self._n

    @property
    def tool_call_count(self) -> int:
        return self._n
