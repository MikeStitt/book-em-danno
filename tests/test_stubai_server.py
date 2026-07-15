"""Tier A tests for the stub AI (`book_em_danno.stubai`): script engine, per-dialect wire
framing, discovery routing, and the transcript schema. No Docker, no live model — a plain
`urllib` client dials the stub directly. See `.docs/plan-runaway-gates-validation.md` §GV0.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from book_em_danno.capture.usage import extract_usage
from book_em_danno.stubai import Drip, Finish, StubConfig, ToolCall, ToolLoop, stub_ai
from book_em_danno.stubai.server import Stub

pytestmark = pytest.mark.timeout(30)


@contextmanager
def _stub(script: list, tmp_path: Path) -> Iterator[Stub]:
    with stub_ai(StubConfig(script=script, transcript_file=tmp_path / "transcript.jsonl")) as stub:
        yield stub


def _post(stub: Stub, path: str, payload: dict) -> tuple[str, bytes]:
    req = urllib.request.Request(
        f"{stub.base_url}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.headers.get("Content-Type", ""), resp.read()


def _get(stub: Stub, path: str) -> tuple[int, bytes]:
    req = urllib.request.Request(f"{stub.base_url}{path}", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# --- chat-completions -------------------------------------------------------


def test_chat_completions_nonstream_carries_usage(tmp_path: Path) -> None:
    with _stub([Finish("hello world")], tmp_path) as stub:
        ctype, raw = _post(stub, "/v1/chat/completions", {"model": "stub", "stream": False})
    assert ctype.startswith("application/json")
    body = json.loads(raw)
    assert body["choices"][0]["message"]["content"] == "hello world"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert extract_usage(body) == {"prompt": 10, "completion": 5, "total": 15, "cached": None}


def test_chat_completions_sse_with_include_usage(tmp_path: Path) -> None:
    with _stub([Finish("hi there")], tmp_path) as stub:
        ctype, raw = _post(
            stub,
            "/v1/chat/completions",
            {"model": "stub", "stream": True, "stream_options": {"include_usage": True}},
        )
    text = raw.decode()
    assert ctype.startswith("text/event-stream")
    assert text.rstrip().endswith("data: [DONE]")
    assert extract_usage(text) == {"prompt": 10, "completion": 5, "total": 15, "cached": None}
    assert _sse_content(text) == "hi there"


def test_chat_completions_sse_without_usage_is_usage_blind(tmp_path: Path) -> None:
    # claurst's known gap: an SSE stream with no `stream_options.include_usage` carries no
    # usage chunk — the exact input the F1 gate-sensor blindness rides on.
    with _stub([Finish("hi")], tmp_path) as stub:
        _ctype, raw = _post(stub, "/v1/chat/completions", {"model": "stub", "stream": True})
    text = raw.decode()
    assert "usage" not in text
    assert extract_usage(text) is None


def test_chat_tool_call_shape(tmp_path: Path) -> None:
    with _stub([ToolCall("read_file", {"path": "a.py"})], tmp_path) as stub:
        _ctype, raw = _post(stub, "/v1/chat/completions", {"model": "stub", "stream": False})
    body = json.loads(raw)
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    call = body["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "read_file"
    assert json.loads(call["function"]["arguments"]) == {"path": "a.py"}


# --- Ollama native ----------------------------------------------------------


def test_ollama_native_nonstream_has_no_usage_block(tmp_path: Path) -> None:
    with _stub([Finish("done")], tmp_path) as stub:
        _ctype, raw = _post(stub, "/api/chat", {"model": "stub", "stream": False})
    body = json.loads(raw)
    assert body["done"] is True
    assert body["eval_count"] == 5 and body["prompt_eval_count"] == 10
    assert "usage" not in body
    assert extract_usage(body) is None  # F1: Ollama native is invisible to the sensor today


def test_ollama_native_ndjson_stream(tmp_path: Path) -> None:
    with _stub([Finish("hi there")], tmp_path) as stub:
        ctype, raw = _post(stub, "/api/chat", {"model": "stub", "stream": True})
    assert ctype.startswith("application/x-ndjson")
    lines = [json.loads(line) for line in raw.decode().splitlines() if line]
    assert lines[-1]["done"] is True
    assert lines[-1]["eval_count"] == 5
    content = "".join(line["message"]["content"] for line in lines if not line["done"])
    assert content == "hi there"
    assert extract_usage(raw.decode()) is None  # NDJSON has no `data:` lines


# --- Responses API ----------------------------------------------------------


def test_responses_api_sse(tmp_path: Path) -> None:
    with _stub([Finish("hi")], tmp_path) as stub:
        ctype, raw = _post(stub, "/v1/responses", {"model": "stub"})
    text = raw.decode()
    assert ctype.startswith("text/event-stream")
    assert "response.completed" in text
    assert extract_usage(text) == {"prompt": 10, "completion": 5, "total": 15, "cached": None}


# --- Anthropic Messages -----------------------------------------------------


def test_anthropic_messages_nonstream(tmp_path: Path) -> None:
    with _stub([Finish("hi")], tmp_path) as stub:
        _ctype, raw = _post(stub, "/v1/messages", {"model": "stub"})
    body = json.loads(raw)
    assert body["content"][0]["text"] == "hi"
    # Anthropic reports input/output but no `total_tokens`; the summed total is derived
    # downstream by `usage.total_tokens`, not carried on the wire.
    assert extract_usage(body) == {"prompt": 10, "completion": 5, "total": None, "cached": None}


# --- script engine ----------------------------------------------------------


def test_tool_loop_answers_every_request(tmp_path: Path) -> None:
    with _stub([ToolLoop("read_file", n=None)], tmp_path) as stub:
        for _ in range(5):
            _ctype, raw = _post(stub, "/v1/chat/completions", {"model": "stub", "stream": False})
            assert json.loads(raw)["choices"][0]["finish_reason"] == "tool_calls"
        assert stub.completion_requests() == 5


def test_bounded_tool_loop_then_advances(tmp_path: Path) -> None:
    with _stub([ToolLoop("read_file", n=2), Finish("all done")], tmp_path) as stub:
        finishes = [
            json.loads(_post(stub, "/v1/chat/completions", {"stream": False})[1]) for _ in range(3)
        ]
    reasons = [f["choices"][0]["finish_reason"] for f in finishes]
    assert reasons == ["tool_calls", "tool_calls", "stop"]
    assert finishes[-1]["choices"][0]["message"]["content"] == "all done"


def test_overrun_settles_to_finish(tmp_path: Path) -> None:
    with _stub([Finish("only step")], tmp_path) as stub:
        first = json.loads(_post(stub, "/v1/chat/completions", {"stream": False})[1])
        second = json.loads(_post(stub, "/v1/chat/completions", {"stream": False})[1])
    assert first["choices"][0]["message"]["content"] == "only step"
    assert second["choices"][0]["finish_reason"] == "stop"  # over-run still terminates


# --- discovery + transcript -------------------------------------------------


def test_discovery_does_not_consume_a_step(tmp_path: Path) -> None:
    with _stub([Finish("real answer")], tmp_path) as stub:
        status, raw = _get(stub, "/api/tags")
        assert status == 200 and "models" in json.loads(raw)
        assert stub.completion_requests() == 0  # a GET /api/tags is not an inference round
        body = json.loads(_post(stub, "/v1/chat/completions", {"stream": False})[1])
    assert body["choices"][0]["message"]["content"] == "real answer"


def test_unknown_route_is_recorded_404(tmp_path: Path) -> None:
    with _stub([Finish("x")], tmp_path) as stub:
        status, _raw = _get(stub, "/no/such/route")
        assert status == 404
        paths = [r["path"] for r in stub.records() if r["direction"] == "request"]
    assert "/no/such/route" in paths


def test_transcript_schema_matches_capture(tmp_path: Path) -> None:
    with _stub([ToolCall("read_file"), Finish("bye")], tmp_path) as stub:
        _post(stub, "/v1/chat/completions", {"model": "m1", "stream": False})
        _post(stub, "/v1/chat/completions", {"model": "m1", "stream": False})
        records = stub.records()
    reqs = [r for r in records if r["direction"] == "request"]
    resps = [r for r in records if r["direction"] == "response"]
    assert len(reqs) == 2 and len(resps) == 2
    for r in records:
        assert {"seq", "direction", "ts", "body"} <= set(r)
    assert stub.completion_requests(model="m1") == 2
    assert stub.completion_requests(model="other") == 0


# --- drip latency injection -------------------------------------------------


def test_drip_streams_content_over_time(tmp_path: Path) -> None:
    with _stub([Drip("one two three four", tokens_per_s=20.0)], tmp_path) as stub:
        start = time.monotonic()
        ctype, raw = _post(stub, "/v1/chat/completions", {"stream": True})
        elapsed = time.monotonic() - start
    assert ctype.startswith("text/event-stream")
    assert _sse_content(raw.decode()) == "one two three four"
    # four whitespace-delimited tokens at 20/s ≈ 0.2 s of injected delay; loose lower
    # bound only (upper bound would be flaky under load).
    assert elapsed >= 0.1


# --- helpers ----------------------------------------------------------------


def _sse_content(sse: str) -> str:
    """Concatenate chat-completions SSE `delta.content` fragments into the reply text."""
    out = []
    for line in sse.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        chunk = json.loads(data)
        for choice in chunk.get("choices", []):
            content = choice.get("delta", {}).get("content")
            if content:
                out.append(content)
    return "".join(out)
