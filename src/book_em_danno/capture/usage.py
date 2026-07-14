"""Extract a model call's token `usage` from a captured response body, agnostic to the
wire format (OpenAI chat-completions, the Responses API, or Anthropic Messages).

Lives at the `capture` layer so the live gate tally (`capture.proxy`, which counts tokens
per response as they stream through) has a low-level, dependency-free extractor. This is
the SINGLE source of truth: `danno_validator.telemetry.wire_metrics` imports `extract_usage`
from here for its post-hoc metrics — the Responses-API SSE parsing is fiddly enough that a
second copy would drift.
"""

from __future__ import annotations

import json
from typing import Any


def normalize_usage(usage: dict) -> dict[str, int | None]:
    """A model call's `usage` block → `{prompt, completion, total, cached}`, agnostic to
    which wire format produced it. The token-count keys differ by API:

    - chat-completions (OpenAI/Ollama/NVIDIA): `prompt_tokens` / `completion_tokens`
    - Responses API (o-series via `@ai-sdk/openai`) & Anthropic: `input_tokens` /
      `output_tokens`

    `cached` (prompt tokens served from cache) reads OpenAI chat's
    `prompt_tokens_details.cached_tokens`, the Responses API's
    `input_tokens_details.cached_tokens`, or Anthropic's `cache_read_input_tokens`."""
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    completion = usage.get("completion_tokens")
    if completion is None:
        completion = usage.get("output_tokens")
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens")
    if cached is None:
        cached = usage.get("cache_read_input_tokens")
    return {
        "prompt": prompt,
        "completion": completion,
        "total": usage.get("total_tokens"),
        "cached": cached,
    }


def chunk_usage(chunk: dict) -> dict | None:
    """The `usage` block carried by one SSE data chunk, whichever format it is:
    chat-completions puts `usage` at the top level of the final chunk; the Responses
    API nests it in the `response` object on the `response.completed` event."""
    usage = chunk.get("usage")
    if isinstance(usage, dict):
        return usage
    resp = chunk.get("response")
    if isinstance(resp, dict) and isinstance(resp.get("usage"), dict):
        return resp["usage"]
    return None


def extract_usage(body: Any) -> dict[str, int | None] | None:
    """Pull normalized `usage` from a response body — a parsed JSON object (non-stream,
    both chat-completions and Responses carry `usage` at the top level) or an SSE text
    blob (`data: {…}` lines; the last chunk carrying `usage` wins)."""
    if isinstance(body, dict):
        usage = body.get("usage")
        return normalize_usage(usage) if isinstance(usage, dict) else None
    if isinstance(body, str):
        found: dict[str, int | None] | None = None
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage = chunk_usage(chunk) if isinstance(chunk, dict) else None
            if usage is not None:
                found = normalize_usage(usage)
        return found
    return None


def total_tokens(usage: dict[str, int | None]) -> int | None:
    """The call's total tokens: the explicit `total` when the API reports it, else the
    sum of prompt + completion. `None` only when neither prompt nor completion is known."""
    if usage.get("total") is not None:
        return usage["total"]
    prompt, completion = usage.get("prompt"), usage.get("completion")
    if prompt is None and completion is None:
        return None
    return (prompt or 0) + (completion or 0)
