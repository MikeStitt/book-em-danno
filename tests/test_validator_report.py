"""Unit tests for the MyST report renderer — pure string rendering, no sandbox."""

from __future__ import annotations

from pathlib import Path

from book_em_danno.core.exec import CaptureResult
from danno_validator.driver import OpencodeTurn
from danno_validator.level0 import ConversationResult, TurnRecord
from danno_validator.level1 import TaskResult
from danno_validator.level2 import DevTaskResult, TestRun
from danno_validator.matrix import ConfigVariant
from danno_validator.oracle import FailureClass, classify_turn
from danno_validator.report import (
    render_level0_page,
    render_matrix_index,
    slug,
    strip_ansi,
    write_level0_page,
    write_sweep_report,
)
from danno_validator.sweep import SweepResult


def _record(label: str, text: str, *, expects_action: bool, side_effect: bool) -> TurnRecord:
    events = [
        {"type": "text", "sessionID": "s", "part": {"type": "text", "text": text}},
        {
            "type": "step_finish",
            "sessionID": "s",
            "part": {"reason": "stop", "tokens": {"total": 5}},
        },
    ]
    turn = OpencodeTurn(result=CaptureResult([], 0, "", ""), events=events, raw=text)
    verdict = classify_turn(turn, side_effect=side_effect, expects_action=expects_action)
    return TurnRecord(label=label, prompt="do it", turn=turn, verdict=verdict, latency_s=1.2)


def _stall_result() -> ConversationResult:
    r = ConversationResult(
        model="ollama/gemma3:27b",
        sandbox="danno-box",
        workspace_root=Path("/tmp/ws"),
        session_id="ses_1",
        overall=FailureClass.STALL,
    )
    r.records = [
        _record("greet", "Hi, I help with code.", expects_action=False, side_effect=False),
        _record("task", "I'll create the file now.", expects_action=True, side_effect=False),
        _record("nudge", "I'll do it right away.", expects_action=True, side_effect=False),
    ]
    return r


def test_slug_normalizes_model_id() -> None:
    assert slug("ollama/gemma3:27b") == "ollama-gemma3-27b"


def test_strip_ansi_removes_escapes() -> None:
    assert strip_ansi("\x1b[31mred\x1b[0m text") == "red text"


def test_render_contains_verdict_and_turns() -> None:
    page = render_level0_page(_stall_result())
    assert "# Level 0 — `ollama/gemma3:27b`" in page
    assert "stall (promised-but-didn't-act)" in page
    assert "### Turn: greet" in page
    assert "### Turn: task" in page
    assert "### Turn: nudge" in page
    assert "promised action: yes" in page  # the task turn promised


def test_render_fences_excerpt() -> None:
    page = render_level0_page(_stall_result(), opencode_jsonc_excerpt='{"model": "x"}')
    assert "opencode.jsonc (excerpt)" in page
    assert '{"model": "x"}' in page


def test_write_level0_page_creates_slugged_file(tmp_path: Path) -> None:
    path = write_level0_page(_stall_result(), tmp_path / "reports")
    assert path.name == "level0-ollama-gemma3-27b.md"
    assert path.is_file()
    assert "# Level 0" in path.read_text()


def _pass_result(model: str) -> ConversationResult:
    r = ConversationResult(
        model=model,
        sandbox="danno-box",
        workspace_root=Path("/tmp/ws"),
        session_id="ses_2",
        overall=FailureClass.PASS,
    )
    r.records = [_record("greet", "Hi.", expects_action=False, side_effect=False)]
    return r


def _sweep() -> list[SweepResult]:
    return [
        SweepResult(
            variant=ConfigVariant("gemma", "ollama/gemma3:27b", "ollama/gemma3:27b"),
            result=_stall_result(),
        ),
        SweepResult(
            variant=ConfigVariant("gptoss", "ollama/gpt-oss:20b", "ollama/gpt-oss:20b"),
            result=_pass_result("ollama/gpt-oss:20b"),
        ),
    ]


def test_matrix_index_has_row_per_config_and_toctree() -> None:
    page = render_matrix_index(_sweep(), ["level0-ollama-gemma3-27b", "level0-ollama-gpt-oss-20b"])
    assert "2 config(s) swept · 1 passed · 1 failed." in page
    assert "| `gemma` | `ollama/gemma3:27b` |" in page
    assert "| `gptoss` | `ollama/gpt-oss:20b` |" in page
    # taxonomy summary counts both classes
    assert "`stall`: 1" in page
    assert "`pass`: 1" in page
    # toctree links the per-config pages
    assert "```{toctree}" in page
    assert "level0-ollama-gemma3-27b" in page


def test_write_sweep_report_prunes_stale_pages(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    # A leftover page from a prior run with a different model set, plus an
    # unrelated file the writer must not touch.
    (out / "level0-claude-opus-4-8.md").write_text("stale")
    (out / "notes.md").write_text("keep me")

    pages, _ = write_sweep_report(_sweep(), out)

    written = {p.name for p in pages}
    assert "level0-claude-opus-4-8.md" not in written
    assert not (out / "level0-claude-opus-4-8.md").exists()  # orphan pruned
    assert (out / "notes.md").read_text() == "keep me"  # non-page file untouched


def test_write_sweep_report_writes_pages_and_matching_index(tmp_path: Path) -> None:
    pages, index = write_sweep_report(_sweep(), tmp_path / "out")
    assert {p.name for p in pages} == {
        "level0-ollama-gemma3-27b.md",
        "level0-ollama-gpt-oss-20b.md",
    }
    assert index.name == "index.md"
    index_text = index.read_text()
    # The toctree references exactly the stems that were written.
    for p in pages:
        assert p.stem in index_text


def _task_result(model: str, *, side_effect: bool) -> TaskResult:
    events = [
        {"type": "text", "sessionID": "s", "part": {"type": "text", "text": "wrote it"}},
        {
            "type": "tool",
            "sessionID": "s",
            "part": {"type": "tool", "tool": "bash", "state": {"status": "completed"}},
        },
        {"type": "step_finish", "sessionID": "s", "part": {"reason": "stop"}},
    ]
    turn = OpencodeTurn(result=CaptureResult([], 0, "", ""), events=events, raw="wrote it")
    verdict = classify_turn(turn, side_effect=side_effect, expects_action=True)
    return TaskResult(
        model=model,
        sandbox="danno-box",
        workspace_root=Path("/tmp/ws"),
        task_label="line-count",
        session_id="s",
        turn=turn,
        verdict=verdict,
        latency_s=2.0,
    )


def _dev_result(model: str, *, side_effect: bool, returncode: int = 0) -> DevTaskResult:
    events = [
        {"type": "text", "sessionID": "s", "part": {"type": "text", "text": "implemented it"}},
        {
            "type": "tool",
            "sessionID": "s",
            "part": {"type": "tool", "tool": "edit", "state": {"status": "completed"}},
        },
        {"type": "step_finish", "sessionID": "s", "part": {"reason": "stop"}},
    ]
    turn = OpencodeTurn(result=CaptureResult([], 0, "", ""), events=events, raw="implemented it")
    verdict = classify_turn(turn, side_effect=side_effect, expects_action=True)
    return DevTaskResult(
        model=model,
        sandbox="danno-box",
        workspace_root=Path("/tmp/ws"),
        task_label="fizzbuzz",
        session_id="s",
        turn=turn,
        verdict=verdict,
        test_run=TestRun(
            command="python3 hidden_test_fizzbuzz.py",
            returncode=returncode,
            stdout="ok — 12 cases passed" if returncode == 0 else "AssertionError",
            stderr="",
        ),
        latency_s=3.0,
    )


def test_matrix_index_has_l2_column_with_skip_dash() -> None:
    sweep = [
        SweepResult(
            variant=ConfigVariant("gemma", "ollama/gemma3:27b", "ollama/gemma3:27b"),
            result=_stall_result(),  # L0 stalled → L1 and L2 skipped
        ),
        SweepResult(
            variant=ConfigVariant("gptoss", "ollama/gpt-oss:20b", "ollama/gpt-oss:20b"),
            result=_pass_result("ollama/gpt-oss:20b"),
            level1=_task_result("ollama/gpt-oss:20b", side_effect=True),  # L1 passed
            level2=_dev_result("ollama/gpt-oss:20b", side_effect=True),  # L2 passed
        ),
    ]
    page = render_matrix_index(sweep, ["level0-ollama-gemma3-27b", "level0-ollama-gpt-oss-20b"])
    assert "| L0 verdict | L1 verdict | L2 verdict |" in page
    # The stalled config shows dashes for both skipped higher tiers.
    assert "| ✗ stall (promised-but-didn't-act) | — | — |" in page
    # The passing config shows L1 and L2 pass badges.
    assert "| ✓ pass | ✓ pass | ✓ pass |" in page


def test_render_page_appends_level2_section_when_present() -> None:
    page = render_level0_page(
        _pass_result("ollama/gpt-oss:20b"),
        level1=_task_result("ollama/gpt-oss:20b", side_effect=True),
        level2=_dev_result("ollama/gpt-oss:20b", side_effect=True),
    )
    assert "## Level 1 — tool/bash" in page
    assert "## Level 2 — software dev" in page
    assert "task `fizzbuzz`" in page
    assert "hidden tests: passed" in page
    assert "ok — 12 cases passed" in page  # test output is shown


def test_render_page_level2_section_shows_failure_output() -> None:
    page = render_level0_page(
        _pass_result("ollama/gpt-oss:20b"),
        level1=_task_result("ollama/gpt-oss:20b", side_effect=True),
        level2=_dev_result("ollama/gpt-oss:20b", side_effect=False, returncode=1),
    )
    assert "hidden tests: failed" in page
    assert "exit 1" in page
    assert "AssertionError" in page


def test_render_page_omits_level2_section_when_absent() -> None:
    page = render_level0_page(_pass_result("ollama/gpt-oss:20b"))
    assert "## Level 2 — software dev" not in page


def test_matrix_index_has_l1_column_with_skip_dash() -> None:
    sweep = [
        SweepResult(
            variant=ConfigVariant("gemma", "ollama/gemma3:27b", "ollama/gemma3:27b"),
            result=_stall_result(),  # L0 stalled → L1 skipped
        ),
        SweepResult(
            variant=ConfigVariant("gptoss", "ollama/gpt-oss:20b", "ollama/gpt-oss:20b"),
            result=_pass_result("ollama/gpt-oss:20b"),
            level1=_task_result("ollama/gpt-oss:20b", side_effect=True),  # L1 passed
        ),
    ]
    page = render_matrix_index(sweep, ["level0-ollama-gemma3-27b", "level0-ollama-gpt-oss-20b"])
    assert "| L0 verdict | L1 verdict |" in page
    # The stalled config shows a dash in the L1 column (tier-1 was skipped).
    assert "| ✗ stall (promised-but-didn't-act) | — |" in page
    # The passing config shows an L1 pass badge.
    assert "| ✓ pass | ✓ pass |" in page


def test_render_page_appends_level1_section_when_present() -> None:
    page = render_level0_page(
        _pass_result("ollama/gpt-oss:20b"),
        level1=_task_result("ollama/gpt-oss:20b", side_effect=True),
    )
    assert "## Level 0 — liveness" in page
    assert "## Level 1 — tool/bash" in page
    assert "task `line-count`" in page
    assert "`bash`" in page  # the tool call is listed


def test_render_page_omits_level1_section_when_absent() -> None:
    page = render_level0_page(_pass_result("ollama/gpt-oss:20b"))
    assert "## Level 1 — tool/bash" not in page


def test_matrix_index_flags_baseline_row_and_excludes_it_from_tally() -> None:
    sweep = [
        SweepResult(
            variant=ConfigVariant("gemma", "ollama/gemma3:27b", "ollama/gemma3:27b"),
            result=_stall_result(),
        ),
        SweepResult(
            variant=ConfigVariant("claude-code", "claude-code (baseline)", "baseline"),
            result=_pass_result("claude-code"),
            level1=_task_result("claude-code", side_effect=True),
            level2=_dev_result("claude-code", side_effect=True),
        ),
    ]
    page = render_matrix_index(sweep, ["level0-ollama-gemma3-27b", "level0-claude-code"])
    # Only the local model counts as a swept config; the baseline is the reference.
    assert "1 config(s) swept · 0 passed · 1 failed." in page
    assert "+ Claude Code baseline (reference row)." in page
    # The baseline row is rendered and flagged.
    assert "| `claude-code` _(baseline)_ | `claude-code (baseline)` |" in page
    # Taxonomy describes the models under test, so the baseline's pass is excluded.
    assert "`pass`" not in page
    assert "`stall`: 1" in page
