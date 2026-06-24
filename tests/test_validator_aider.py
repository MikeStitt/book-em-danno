"""Unit tests for the M5 Aider Polyglot suite — `load_aider_task` parsing of the
Exercism layout and `run_aider_suite`'s seed/run/grade loop. No Docker / no network:
the exercise tree is fabricated in tmp_path and the in-VM `capture_exec` is stubbed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from book_em_danno.core.exec import CaptureResult, Runner
from danno_validator.suites import aider, run
from danno_validator.suites.aider import load_aider_task


def _make_exercise(
    root: Path,
    *,
    lang: str = "python",
    slug: str = "anagram",
    instructions: str = "Find the anagrams.",
) -> None:
    base = root / lang / "exercises" / "practice" / slug
    (base / ".meta").mkdir(parents=True)
    (base / ".docs").mkdir(parents=True)
    (base / ".meta" / "config.json").write_text(
        json.dumps(
            {"files": {"solution": [f"{slug}.py"], "test": [f"{slug}_test.py"], "example": []}}
        )
    )
    (base / f"{slug}.py").write_text("def solve():\n    pass\n")
    (base / f"{slug}_test.py").write_text("def test_it():\n    assert solve()\n")
    (base / ".docs" / "instructions.md").write_text(instructions)


def test_load_aider_task_parses_exercism_layout(tmp_path: Path) -> None:
    _make_exercise(tmp_path)
    task = load_aider_task(tmp_path, "python/anagram")
    assert task.id == "python/anagram"
    assert task.language == "python"
    assert [p for p, _ in task.solution_files] == ["anagram.py"]
    assert [p for p, _ in task.test_files] == ["anagram_test.py"]
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


def _stub_capture(monkeypatch: pytest.MonkeyPatch, *, grade_ok: bool) -> list[str]:
    """Stub the in-VM `capture_exec` used by AiderTask.provision/grade."""
    seen: list[str] = []

    def fake(runner, name, command, *, check=False):  # type: ignore[no-untyped-def]
        seen.append(command)
        rc = 0 if (grade_ok or "pytest" not in command) else 1
        return CaptureResult([command], rc, "", "")

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
    # The stub solution + test were seeded into the per-exercise subdir.
    assert (ws / "anagram" / "anagram.py").is_file()
    assert (ws / "anagram" / "anagram_test.py").is_file()
    # The turn's cwd was bound to the exercise subdir.
    assert turns[0]["workspace"] == ws / "anagram"
    # pytest was the grading command.
    assert any("pytest" in c for c in commands)


def test_run_aider_suite_grade_fail_is_non_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "polyglot"
    _make_exercise(checkout)
    ws = tmp_path / "ws"
    ws.mkdir()
    _stub_capture(monkeypatch, grade_ok=False)
    verdicts = run.run_aider_suite(
        Runner(),
        "box",
        checkout=checkout,
        select=["python/anagram"],
        workspace=ws,
        run_turn=lambda r, n, p, **kw: _FakeTurn(n_tools=0),
        model=None,
    )
    assert verdicts[0].passed is False


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
