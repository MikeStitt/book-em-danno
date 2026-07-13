"""Unit tests for `telemetry.wire_metrics`: deriving token/context/latency metrics from
a permutation's `--capture` wire JSONL, and the readable-transcript renderer."""

from __future__ import annotations

import json
from pathlib import Path

from danno_validator.telemetry import wire_metrics as wm


def _resp_body(prompt: int, completion: int, cached: int = 0) -> dict:
    return {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


def _pair(
    seq: int, req_ts: float, resp_ts: float, body: object, path: str = "/v1/chat/completions"
):
    return [
        {
            "seq": seq,
            "direction": "request",
            "ts": req_ts,
            "method": "POST",
            "path": path,
            "body": {"model": "m", "messages": []},
        },
        {"seq": seq, "direction": "response", "ts": resp_ts, "status": 200, "body": body},
    ]


def test_parse_and_rollup_token_split_and_rtt() -> None:
    records = [
        *_pair(1, 100.0, 102.0, _resp_body(4000, 100, cached=1000)),
        *_pair(2, 102.0, 105.0, _resp_body(4200, 200, cached=1200)),
    ]
    metrics = wm.parse_capture_records(records)
    assert [m.rtt_s for m in metrics] == [2.0, 3.0]
    assert [m.prompt_tokens for m in metrics] == [4000, 4200]
    roll = wm.rollup(metrics)
    assert roll.input_tokens == 8200
    assert roll.output_tokens == 300
    assert roll.cached_tokens == 2200
    assert roll.total_tokens == 8500
    # overall tok/s = 300 completion / 5.0s generation
    assert roll.tok_per_s == 60.0
    # §2.2 TTFT = first call whole-response time, labelled
    assert roll.ttft_s == 2.0 and roll.ttft_label == wm.TTFT_LABEL
    # §6.1/§6.2 context curve + deltas; §6.3 peak
    assert roll.ctx_growth == [4000, 4200]
    assert roll.ctx_deltas == [200]
    assert roll.peak_ctx_tokens == 4200
    assert roll.rtt_min_s == 2.0 and roll.rtt_max_s == 3.0 and roll.rtt_mean_s == 2.5


def test_non_inference_calls_are_skipped() -> None:
    # An /api/tags-style call with no usage must not become a metric row.
    records = [
        *_pair(1, 1.0, 1.1, {"models": []}, path="/api/tags"),
        *_pair(2, 2.0, 4.0, _resp_body(10, 5)),
    ]
    metrics = wm.parse_capture_records(records)
    assert [m.seq for m in metrics] == [2]


def test_occ_style_wire_yields_nonzero_totals() -> None:
    # §1.3: occ's stream-json reports tokens==0, but the wire usage is harness-agnostic,
    # so parsing the capture gives real totals regardless of which agent produced it.
    records = _pair(1, 0.0, 1.0, _resp_body(1234, 56))
    roll = wm.rollup(wm.parse_capture_records(records))
    assert roll.input_tokens == 1234 and roll.output_tokens == 56


def test_sse_streaming_body_usage_extracted() -> None:
    sse = (
        'data: {"choices":[{"delta":{"content":"hi"}}]}\n'
        'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":7,"completion_tokens":3,'
        '"total_tokens":10}}\n'
        "data: [DONE]\n"
    )
    usage = wm._extract_usage(sse)
    assert usage == {"prompt": 7, "completion": 3, "total": 10, "cached": None}


def test_cache_read_input_tokens_alias() -> None:
    # Anthropic-shaped cached field is read when the OpenAI details block is absent.
    usage = wm._normalize_usage({"prompt_tokens": 5, "cache_read_input_tokens": 2})
    assert usage["cached"] == 2


def test_responses_api_usage_key_aliases() -> None:
    # Responses API / Anthropic use input_tokens/output_tokens and the input_tokens_details
    # cached block — normalized to the same canonical shape as chat-completions.
    usage = wm._normalize_usage(
        {
            "input_tokens": 10821,
            "output_tokens": 312,
            "total_tokens": 11133,
            "input_tokens_details": {"cached_tokens": 64},
        }
    )
    assert usage == {"prompt": 10821, "completion": 312, "total": 11133, "cached": 64}


def test_responses_api_non_stream_dict_usage() -> None:
    # A non-streaming Responses body carries usage at the top level, input_tokens-shaped.
    body = {
        "object": "response",
        "output": [],
        "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    }
    got = wm._extract_usage(body)
    assert got == {"prompt": 100, "completion": 20, "total": 120, "cached": None}


def test_responses_api_sse_usage_from_response_completed() -> None:
    # Responses API nests usage in the `response` object on `response.completed`; earlier
    # events carry usage: null and must not zero it out (last non-null wins).
    sse = (
        'data: {"type":"response.created","response":{"usage":null}}\n'
        'data: {"type":"response.in_progress","response":{"usage":null}}\n'
        'data: {"type":"response.function_call_arguments.delta","delta":"{"}\n'
        'data: {"type":"response.completed","response":{"usage":'
        '{"input_tokens":10821,"output_tokens":312,"total_tokens":11133,'
        '"input_tokens_details":{"cached_tokens":0}}}}\n'
    )
    assert wm._extract_usage(sse) == {
        "prompt": 10821,
        "completion": 312,
        "total": 11133,
        "cached": 0,
    }


def test_render_transcript_responses_api_input_and_sse_output() -> None:
    # A Responses-API turn: request uses `input[]`/`instructions` (not `messages`), and the
    # response is an SSE string whose final output[] carries the tool call — both must render.
    sse = (
        'data: {"type":"response.created","response":{"usage":null}}\n'
        'data: {"type":"response.completed","response":{'
        '"output":['
        '{"type":"reasoning","summary":[]},'
        '{"type":"function_call","name":"glob","arguments":"{\\"pattern\\":\\"*.py\\"}"}'
        "],"
        '"usage":{"input_tokens":50,"output_tokens":9,"total_tokens":59}}}\n'
    )
    records = [
        {
            "seq": 1,
            "direction": "request",
            "ts": 0.0,
            "method": "POST",
            "path": "/v1/responses",
            "body": {
                "model": "o4-mini",
                "instructions": "You are opencode",
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": "make a roster"}]}
                ],
                "tools": [{}, {}],
            },
        },
        {"seq": 1, "direction": "response", "ts": 1.0, "status": 200, "body": sse},
    ]
    md = wm.render_transcript(records)
    assert "You are opencode" in md  # instructions rendered as the system prompt
    assert "make a roster" in md  # input_text block flattened to text
    assert "tool_call: `glob({" in md  # final output[] function_call rendered
    assert "usage: prompt=50 completion=9" in md  # usage read from response.completed


def test_headroom_pct() -> None:
    assert wm.headroom_pct(4000, 40000) == 90.0
    assert wm.headroom_pct(None, 40000) is None
    assert wm.headroom_pct(4000, 0) is None


def test_empty_capture_is_zero_metrics() -> None:
    roll = wm.rollup([])
    assert roll.request_count == 0 and roll.input_tokens == 0 and roll.peak_ctx_tokens is None


def test_metrics_from_files_reads_jsonl(tmp_path: Path) -> None:
    cap = tmp_path / "c.jsonl"
    lines = _pair(1, 0.0, 2.0, _resp_body(100, 20))
    cap.write_text("\n".join(json.dumps(r) for r in lines) + "\n", encoding="utf-8")
    roll = wm.metrics_from_files([cap])
    assert roll.input_tokens == 100 and roll.output_tokens == 20


def test_render_transcript_is_readable_and_redaction_safe() -> None:
    records = [
        {
            "seq": 1,
            "direction": "request",
            "ts": 0.0,
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": {"Authorization": "<redacted>"},  # proxy already redacted
            "body": {
                "model": "qwen",
                "messages": [{"role": "system", "content": "You are helpful"}],
                "tools": [{}, {}],
            },
        },
        {"seq": 1, "direction": "response", "ts": 1.0, "status": 200, "body": _resp_body(10, 5)},
    ]
    md = wm.render_transcript(records)
    assert "Call 1" in md
    assert "You are helpful" in md  # the system prompt is dumped
    assert "tools offered: 2" in md
    assert "usage: prompt=10 completion=5" in md
    assert "<redacted>" not in md  # headers aren't rendered; no secret can surface
