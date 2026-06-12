"""Slow, opt-in contract tests pinning Ollama's OpenAI-compatible `/v1` semantics.

These exercise the verified facts danno's generator now depends on (research doc
§6): `/v1` honors `reasoning_effort`, ignores `think` and a body `num_ctx`, loads
the model at its FULL context, and gemma4 emits a `reasoning` field by default. If
an Ollama upgrade changes any of these, this suite fails loud rather than danno
silently emitting the wrong config.

Host-side only — no Docker, no opencode. They skip when Ollama is unreachable or
`gemma4:26b` is absent. gemma4:26b is used throughout: at full context it is only
~16.9 GiB (sliding-window attention), so it is RAM-safe to load here.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from book_em_danno.commands import ollama

pytestmark = pytest.mark.slow

MODEL = "gemma4:26b"
HOST = ollama.DEFAULT_HOST_URL


def _model_present(tag: str) -> bool:
    try:
        with urllib.request.urlopen(f"{HOST}/api/tags", timeout=2.0) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False
    return any(m.get("name") == tag for m in body.get("models", []))


ollama_down = not ollama.reachable()
model_absent = not _model_present(MODEL)

skip_no_model = pytest.mark.skipif(
    ollama_down or model_absent,
    reason=f"Ollama unreachable or {MODEL} not pulled",
)


def _chat(extra: dict[str, Any]) -> dict[str, Any]:
    """POST /v1/chat/completions with a fixed tiny prompt; return the parsed body.

    `timeout` is generous to cover a cold model load at full context."""
    payload = json.dumps(
        {
            "model": MODEL,
            "messages": [
                {"role": "user", "content": "What is 17*23? Answer with just the number."}
            ],
            "stream": False,
            "max_tokens": 64,
            **extra,
        }
    ).encode()
    req = urllib.request.Request(
        f"{HOST}/v1/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=240) as resp:
        return json.loads(resp.read())


def _message(body: dict[str, Any]) -> dict[str, Any]:
    return body["choices"][0]["message"]


@skip_no_model
def test_v1_default_emits_reasoning_field() -> None:
    # gemma4 returns a non-empty `reasoning` field by default — this is exactly the
    # surface that triggers the opencode #21903 hang.
    msg = _message(_chat({}))
    assert msg.get("reasoning"), f"expected a non-empty reasoning field, got: {msg!r}"


@skip_no_model
def test_v1_reasoning_effort_none_suppresses() -> None:
    body = _chat({"reasoning_effort": "none"})
    reasoning = _message(body).get("reasoning")
    assert not reasoning, f"reasoning should be suppressed, got: {reasoning!r}"
    assert body["usage"]["completion_tokens"] < 30, body["usage"]


@skip_no_model
def test_v1_think_param_is_ignored() -> None:
    # `think` is an Ollama-native param; via /v1 it is inert, so reasoning persists.
    msg = _message(_chat({"think": False}))
    assert msg.get("reasoning"), "think:false should be ignored on /v1 (reasoning still present)"


@skip_no_model
def test_v1_body_num_ctx_is_ignored() -> None:
    # A body num_ctx does not size the /v1 load; the model loads at its full context.
    _chat({"num_ctx": 16384})
    with urllib.request.urlopen(f"{HOST}/api/ps", timeout=10) as resp:
        ps = json.loads(resp.read())
    loaded = next((m for m in ps.get("models", []) if m.get("name") == MODEL), None)
    assert loaded is not None, f"{MODEL} not reported loaded by /api/ps: {ps!r}"
    ctx = loaded.get("context_length")
    assert ctx is not None and ctx != 16384, (
        f"/v1 should ignore body num_ctx and load full context, got context_length={ctx}"
    )
