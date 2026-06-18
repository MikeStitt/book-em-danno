"""Serialize a sweep's results into the `results.json` run record (schema v1).

`results.json` is the validator's one structured artifact — the contract for CI
(`--strict`), dashboards, and host-side re-rendering — so it is a direct, lossless
serialization of the `list[SweepResult]` the harness returns (`SweepResult` →
`ConversationResult` / `TaskResult` / `DevTaskResult` → `TurnVerdict` / `TestRun`),
plus run metadata. The shape is fixed by `.docs/ux-danno-validate-cli.md`; bump
`SCHEMA_VERSION` on any breaking change.

Pure except `write_results_json` (the thin I/O edge): `run_record` builds a
JSON-ready dict from in-memory results, so it is unit-testable without a sandbox.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from danno_validator.baseline import BASELINE_MODEL
from danno_validator.driver import Turn
from danno_validator.level0 import ConversationResult, TurnRecord
from danno_validator.level1 import TaskResult
from danno_validator.level2 import DevTaskResult
from danno_validator.menu import is_recommended, verdict_badge
from danno_validator.oracle import TurnVerdict
from danno_validator.sweep import SweepResult

SCHEMA_VERSION = 1


def _tool_calls(turn: Turn) -> list[dict[str, Any]]:
    """Normalize a turn's tool calls to `[{tool, status}]` across agents, so a
    consumer never branches on opencode-vs-claude transcript shape."""
    return [
        {"tool": c.get("tool"), "status": c.get("state", {}).get("status")} for c in turn.tool_calls
    ]


def _verdict(v: TurnVerdict) -> dict[str, Any]:
    return {
        "failure_class": v.failure_class.value,
        "promised_action": v.promised_action,
        "tool_call_count": v.tool_call_count,
        "side_effect": v.side_effect,
        "rationale": v.rationale,
    }


def _turn_record(r: TurnRecord) -> dict[str, Any]:
    return {
        "label": r.label,
        "prompt": r.prompt,
        "assistant_text": r.turn.assistant_text,
        "tool_calls": _tool_calls(r.turn),
        "tokens": r.turn.tokens,
        "latency_s": round(r.latency_s, 3),
        "errors": r.turn.errors,
        "verdict": _verdict(r.verdict),
    }


def _level0(result: ConversationResult) -> dict[str, Any]:
    return {
        "overall": result.overall.value,
        "passed": result.passed,
        "session_id": result.session_id,
        "tokens": result.total_tokens,
        "cost": result.total_cost,
        "latency_s": round(result.total_latency_s, 3),
        "turns": [_turn_record(r) for r in result.records],
    }


def _task(tr: TaskResult | None) -> dict[str, Any] | None:
    """Level-1 record, or `null` when the tier was short-circuited (never `{}`)."""
    if tr is None:
        return None
    return {
        "task_label": tr.task_label,
        "overall": tr.overall.value,
        "passed": tr.passed,
        "session_id": tr.session_id,
        "tokens": tr.tokens,
        "latency_s": round(tr.latency_s, 3),
        "assistant_text": tr.turn.assistant_text,
        "tool_calls": _tool_calls(tr.turn),
        "errors": tr.turn.errors,
        "verdict": _verdict(tr.verdict),
    }


def _dev(dr: DevTaskResult | None) -> dict[str, Any] | None:
    """Level-2 record (with the in-VM hidden-suite run), or `null` when skipped."""
    if dr is None:
        return None
    return {
        "task_label": dr.task_label,
        "overall": dr.overall.value,
        "passed": dr.passed,
        "session_id": dr.session_id,
        "tokens": dr.tokens,
        "latency_s": round(dr.latency_s, 3),
        "assistant_text": dr.turn.assistant_text,
        "tool_calls": _tool_calls(dr.turn),
        "errors": dr.turn.errors,
        "verdict": _verdict(dr.verdict),
        "test_run": {
            "command": dr.test_run.command,
            "returncode": dr.test_run.returncode,
            "passed": dr.test_run.passed,
            "stdout": dr.test_run.stdout,
            "stderr": dr.test_run.stderr,
        },
    }


def _result(s: SweepResult, *, requested_model: str | None) -> dict[str, Any]:
    is_baseline = s.variant.model_name == BASELINE_MODEL
    record: dict[str, Any] = {
        "model_name": s.variant.model_name,
        "model_ref": s.variant.model_ref,
        "is_baseline": is_baseline,
        "recommended": is_recommended(s),
        "badge": verdict_badge(s),
        "level0": _level0(s.result),
        "level1": _task(s.level1),
        "level2": _dev(s.level2),
    }
    if is_baseline:
        # The baseline alone carries what was asked for vs what claude resolved to
        # (variant.model_ref is the resolved model the harness recorded).
        record["requested_model"] = requested_model
        record["resolved_model"] = s.variant.model_ref
    return record


def run_record(
    results: Sequence[SweepResult],
    *,
    config_path: Path,
    declared_models: Sequence[str],
    run_meta: Mapping[str, Any],
    generated_at: str,
    danno_version: str,
    requested_baseline_model: str | None = None,
) -> dict[str, Any]:
    """Build the `results.json` dict from a sweep's results plus run metadata.

    `results` is the full matrix (swept configs + an optional Claude baseline row);
    the baseline is excluded from `summary` (it describes the models under test),
    exactly as the report's index page does. `run_meta` is the verbatim `run`
    block (what was asked for); `generated_at`/`danno_version` are stamped by the
    caller so this stays pure and deterministic in tests.
    """
    swept = [s for s in results if s.variant.model_name != BASELINE_MODEL]
    baseline = next((s for s in results if s.variant.model_name == BASELINE_MODEL), None)
    taxonomy = Counter(s.result.overall.value for s in swept)
    summary: dict[str, Any] = {
        "swept_total": len(swept),
        "passed_l0": sum(1 for s in swept if s.result.passed),
        "passed_all_tiers": sum(1 for s in swept if is_recommended(s)),
        "taxonomy": dict(taxonomy),
    }
    if baseline is not None:
        summary["baseline"] = {
            "model": baseline.variant.model_ref,
            "passed_all_tiers": is_recommended(baseline),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": "danno-validate",
        "danno_version": danno_version,
        "generated_at": generated_at,
        "config": {"path": str(config_path), "declared_models": list(declared_models)},
        "run": dict(run_meta),
        "results": [_result(s, requested_model=requested_baseline_model) for s in results],
        "summary": summary,
    }


def write_results_json(record: Mapping[str, Any], out_path: Path) -> Path:
    """Write `record` as pretty JSON to `out_path` (parents created). Returns it.

    `ensure_ascii=False` keeps the unicode verdict badges (`✓ · ✗`) readable.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
    return out_path
