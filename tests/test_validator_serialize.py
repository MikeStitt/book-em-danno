"""Unit tests for the results.json run-record serializer — pure, no sandbox."""

from __future__ import annotations

import json
from pathlib import Path

from book_em_danno.core.exec import CaptureResult
from danno_validator.driver import OpencodeTurn
from danno_validator.level0 import ConversationResult, TurnRecord
from danno_validator.level1 import TaskResult
from danno_validator.level2 import DevTaskResult, TestRun
from danno_validator.matrix import ConfigVariant
from danno_validator.oracle import FailureClass, classify_turn
from danno_validator.serialize import SCHEMA_VERSION, run_record, write_results_json
from danno_validator.sweep import SweepResult


def _turn(text: str, *, tool: str | None = None) -> OpencodeTurn:
    events: list[dict] = [
        {"type": "text", "sessionID": "s", "part": {"type": "text", "text": text}}
    ]
    if tool is not None:
        events.append(
            {
                "type": "tool",
                "sessionID": "s",
                "part": {"type": "tool", "tool": tool, "state": {"status": "completed"}},
            }
        )
    events.append({"type": "step_finish", "sessionID": "s", "part": {"reason": "stop"}})
    return OpencodeTurn(result=CaptureResult([], 0, "", ""), events=events, raw=text)


def _l0(overall: FailureClass) -> ConversationResult:
    r = ConversationResult(
        model="ollama/x",
        sandbox="box",
        workspace_root=Path("/tmp/ws"),
        session_id="ses",
        overall=overall,
    )
    turn = _turn("hi")
    verdict = classify_turn(turn, side_effect=False, expects_action=False)
    r.records = [TurnRecord(label="greet", prompt="hi", turn=turn, verdict=verdict, latency_s=1.0)]
    return r


def _l1(*, side_effect: bool) -> TaskResult:
    turn = _turn("wrote it", tool="bash")
    return TaskResult(
        model="ollama/x",
        sandbox="box",
        workspace_root=Path("/tmp/ws"),
        task_label="line-count",
        session_id="ses",
        turn=turn,
        verdict=classify_turn(turn, side_effect=side_effect, expects_action=True),
        latency_s=2.0,
    )


def _l2(*, side_effect: bool, returncode: int = 0) -> DevTaskResult:
    turn = _turn("implemented", tool="edit")
    return DevTaskResult(
        model="ollama/x",
        sandbox="box",
        workspace_root=Path("/tmp/ws"),
        task_label="fizzbuzz",
        session_id="ses",
        turn=turn,
        verdict=classify_turn(turn, side_effect=side_effect, expects_action=True),
        test_run=TestRun(command="python3 t.py", returncode=returncode, stdout="ok", stderr=""),
        latency_s=3.0,
    )


def _variant(name: str, ref: str) -> ConfigVariant:
    return ConfigVariant(name, ref, ref)


def _sweep() -> list[SweepResult]:
    return [
        # all tiers pass
        SweepResult(
            variant=_variant("gptoss", "ollama/gpt-oss:20b"),
            result=_l0(FailureClass.PASS),
            level1=_l1(side_effect=True),
            level2=_l2(side_effect=True),
        ),
        # L0 stalls → L1/L2 short-circuited (None)
        SweepResult(
            variant=_variant("gemma", "ollama/gemma3:27b"),
            result=_l0(FailureClass.STALL),
        ),
        # the Claude baseline row
        SweepResult(
            variant=_variant("claude-code", "claude-opus-4-8"),
            result=_l0(FailureClass.PASS),
            level1=_l1(side_effect=True),
            level2=_l2(side_effect=True),
        ),
    ]


def _record() -> dict:
    return run_record(
        _sweep(),
        config_path=Path("/proj/danno.toml"),
        declared_models=["gptoss", "gemma", "sonnet"],
        run_meta={"max_level": 2, "baseline": {"enabled": True, "requested_model": "opus"}},
        generated_at="2026-06-18T14:30:05Z",
        danno_version="0.3.0",
        requested_baseline_model="opus",
    )


def test_record_top_level_shape() -> None:
    rec = _record()
    assert rec["schema_version"] == SCHEMA_VERSION
    assert rec["tool"] == "danno-validate"
    assert rec["danno_version"] == "0.3.0"
    assert rec["generated_at"] == "2026-06-18T14:30:05Z"
    assert rec["config"] == {
        "path": "/proj/danno.toml",
        "declared_models": ["gptoss", "gemma", "sonnet"],
    }
    assert rec["run"]["max_level"] == 2
    assert len(rec["results"]) == 3


def test_skipped_tiers_are_null_not_empty() -> None:
    gemma = next(r for r in _record()["results"] if r["model_name"] == "gemma")
    assert gemma["level0"]["overall"] == "stall"
    assert gemma["level1"] is None
    assert gemma["level2"] is None


def test_level2_carries_test_run_and_tool_calls() -> None:
    gptoss = next(r for r in _record()["results"] if r["model_name"] == "gptoss")
    assert gptoss["level2"]["test_run"] == {
        "command": "python3 t.py",
        "returncode": 0,
        "passed": True,
        "stdout": "ok",
        "stderr": "",
    }
    assert gptoss["level1"]["tool_calls"] == [{"tool": "bash", "status": "completed"}]
    assert gptoss["recommended"] is True
    assert gptoss["badge"] == "[L0 ✓ · L1 ✓ · L2 ✓]"


def test_baseline_row_carries_requested_and_resolved_model() -> None:
    base = next(r for r in _record()["results"] if r["is_baseline"])
    assert base["model_name"] == "claude-code"
    assert base["requested_model"] == "opus"
    assert base["resolved_model"] == "claude-opus-4-8"
    # non-baseline rows omit those keys
    gptoss = next(r for r in _record()["results"] if r["model_name"] == "gptoss")
    assert "resolved_model" not in gptoss


def test_summary_excludes_baseline_and_counts_taxonomy() -> None:
    summary = _record()["summary"]
    assert summary["swept_total"] == 2  # baseline excluded
    assert summary["passed_l0"] == 1
    assert summary["passed_all_tiers"] == 1
    assert summary["taxonomy"] == {"pass": 1, "stall": 1}
    assert summary["baseline"] == {"model": "claude-opus-4-8", "passed_all_tiers": True}


def test_write_results_json_round_trips(tmp_path: Path) -> None:
    path = write_results_json(_record(), tmp_path / "run" / "results.json")
    assert path.is_file()
    loaded = json.loads(path.read_text())
    assert loaded["schema_version"] == SCHEMA_VERSION
    assert loaded["summary"]["swept_total"] == 2
    # the unicode badge survives (ensure_ascii=False)
    assert "✓" in loaded["results"][0]["badge"]
