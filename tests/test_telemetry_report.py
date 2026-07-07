"""Unit tests for `telemetry.report`: rendering a bench run (bench.json rows carrying
`wire`/`resource`/`sidecars`) into report.md + report.html, and the multi-harness merge."""

from __future__ import annotations

from pathlib import Path

from danno_validator.telemetry import report


def _row(**over: object) -> dict:
    base: dict = {
        "suite": "aider",
        "task": "python/grade-school",
        "model": "ollama/qwen3:latest",
        "passed": True,
        "verdict": "FailureClass.PASS",
        "tool_calls": 3,
        "tokens": 4200,
        "cost": 0.0,
        "latency_s": 12.4,
        "error": None,
        "wire": {
            "request_count": 3,
            "input_tokens": 12000,
            "output_tokens": 300,
            "cached_tokens": 1000,
            "total_tokens": 12300,
            "tok_per_s": 60.0,
            "ttft_s": 2.0,
            "ttft_label": "whole-response (non-streaming)",
            "rtt_min_s": 2.0,
            "rtt_max_s": 3.0,
            "rtt_mean_s": 2.5,
            "peak_ctx_tokens": 4200,
            "ctx_headroom_pct": 89.7,
            "ctx_growth": [4000, 4100, 4200],
            "ctx_deltas": [100, 100],
        },
        "resource": {
            "sample_count": 20,
            "cpu_peak": 82.0,
            "cpu_mean": 40.0,
            "mem_peak_kb": 8_000_000,
            "gpu_util_peak": 95.0,
            "gpu_util_mean": 60.0,
            "vram_peak_mb": 21000.0,
            "model_vram_peak_bytes": 22_020_096_000,
            "model_load_s": 1.5,
        },
        "sidecars": {
            "metrics": "metrics/aider/python-grade-school/ollama-qwen3-latest.json",
            "transcript": "transcripts/aider/python-grade-school/ollama-qwen3-latest.md",
            "captures": ["captures/aider/python-grade-school/ollama-qwen3-latest.ollama.jsonl"],
            "samples": "samples/aider/python-grade-school/ollama-qwen3-latest.jsonl",
        },
    }
    base.update(over)
    return base


def _payload(*rows: dict) -> dict:
    return {
        "generated_at": "2026-07-06T00:00:00Z",
        "harness": "opencode",
        "models": ["ollama/qwen3:latest"],
        "results": list(rows),
    }


def test_markdown_summary_and_detail() -> None:
    md = report.render_markdown(_payload(_row()))
    assert "1/1 passed" in md
    # summary row: token split, tok/s, headroom
    assert "12,000→300 (1,000c)" in md
    assert "60" in md
    assert "89.7% free" in md
    # detail: growth curve + ttft label + sidecar links
    assert "4,000 → 4,100 → 4,200" in md
    assert "whole-response (non-streaming)" in md
    assert "[transcript](transcripts/aider/python-grade-school/ollama-qwen3-latest.md)" in md


def test_html_is_self_contained_and_has_pills() -> None:
    html_doc = report.render_html(
        _payload(_row(), _row(passed=False, verdict="FailureClass.STALL"))
    )
    assert "<style>" in html_doc and "http://" not in html_doc  # no external assets
    assert 'class="pill pass"' in html_doc and 'class="pill fail"' in html_doc
    assert "<svg" in html_doc  # context sparkline rendered
    assert "1/2 passed" in html_doc


def test_missing_wire_and_resource_degrade_to_dashes() -> None:
    # A row with no capture/sample (flat fields only) must still render, no KeyError.
    bare = _row()
    del bare["wire"]
    del bare["resource"]
    del bare["sidecars"]
    md = report.render_markdown(_payload(bare))
    assert "—" in md  # empty metric cells
    html_doc = report.render_html(_payload(bare))
    assert "danno bench" in html_doc


def test_write_report_emits_both_files(tmp_path: Path) -> None:
    md_path, html_path = report.write_report(tmp_path, _payload(_row()))
    assert md_path.is_file() and html_path.is_file()
    assert md_path.read_text(encoding="utf-8").startswith("# danno bench")
    assert "<table>" in html_path.read_text(encoding="utf-8")


def test_provenance_header_rendered(tmp_path: Path) -> None:
    prov = {
        "danno": {"version": "0.10.0", "commit": "abc1234"},
        "host": {
            "cpu_model": "AMD EPYC",
            "cpu_cores": 32,
            "gpus": [{"name": "RTX 6000", "driver": "550", "vram_total_mb": 49140}],
        },
        "sample_interval_s": 0.5,
    }
    md = report.render_markdown(_payload(_row()), prov)
    assert "AMD EPYC (32 cores)" in md
    assert "0.10.0 (abc1234)" in md
    assert "sampler interval: 0.5s" in md


def test_merge_grid_across_harnesses() -> None:
    a = _payload(_row())
    a["harness"] = "opencode"
    b = _payload(_row(passed=False, verdict="FailureClass.STALL"))
    b["harness"] = "occ"
    md = report.merge_markdown([a, b])
    assert "aider/python/grade-school" in md
    assert "✓" in md and "✗" in md
    assert "**passed**" in md
    html_doc = report.merge_html([a, b])
    assert "comparison" in html_doc and 'class="fail tnum"' in html_doc
