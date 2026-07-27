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

# Request-path suffixes that mark an agent-loop inference round (as opposed to discovery /
# health traffic). The round-count sensor (Gate 1) keys on THIS, not on whether the
# response carried a `usage` block, so claurst-local Ollama-native traffic — which reports
# `eval_count`, not `usage` — is counted too (F1). Kept here as the single source both the
# live proxy (`capture.proxy`) and the post-hoc parser (`telemetry.wire_metrics`) import.
_INFERENCE_SUFFIXES = (
    "/chat/completions",  # OpenAI-compatible (opencode /v1, claurst)
    "/responses",  # Responses API (opencode ↔ NVIDIA)
    "/messages",  # Anthropic Messages
    "/api/chat",  # Ollama native (claurst local)
    "/api/generate",  # Ollama native completion
)


def is_inference_request(method: str, path: str) -> bool:
    """Whether a captured request is an agent-loop inference round, decided by method +
    path — independent of what the response contained. `POST` to a known inference
    endpoint counts; every `GET` and every discovery/health path (`/api/tags`,
    `/v1/models`, `/api/show`) does not."""
    if method.upper() != "POST":
        return False
    base = path.split("?", 1)[0]
    return any(base.endswith(suffix) for suffix in _INFERENCE_SUFFIXES)


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


def ollama_usage(obj: dict) -> dict[str, int | None] | None:
    """Normalized usage from an Ollama-native body's `prompt_eval_count`/`eval_count`.

    Ollama's `/api/chat` and `/api/generate` carry NO `usage` block — the token counts
    live in these top-level fields (of the final `done: true` object when streaming). Map
    them so Gate 2 and the token telemetry are non-zero for claurst-local cells (F1), not
    just for the dialects that happen to speak OpenAI's `usage` shape."""
    if "eval_count" not in obj and "prompt_eval_count" not in obj:
        return None
    prompt = obj.get("prompt_eval_count")
    completion = obj.get("eval_count")
    synthetic: dict[str, Any] = {"prompt_tokens": prompt, "completion_tokens": completion}
    if isinstance(prompt, int) and isinstance(completion, int):
        synthetic["total_tokens"] = prompt + completion
    return normalize_usage(synthetic)


def extract_usage(body: Any) -> dict[str, int | None] | None:
    """Pull normalized `usage` from a response body — a parsed JSON object (chat-completions
    and Responses carry `usage` at the top level; Ollama native carries `eval_count`), an
    SSE text blob (`data: {…}` lines), or an Ollama NDJSON stream (bare JSON lines). The
    last chunk carrying token counts wins."""
    if isinstance(body, dict):
        usage = body.get("usage")
        if isinstance(usage, dict):
            return normalize_usage(usage)
        return ollama_usage(body)
    if isinstance(body, str):
        found: dict[str, int | None] | None = None
        for raw_line in body.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                data = line[len("data:") :].strip()
                if not data or data == "[DONE]":
                    continue
                usage = _sse_chunk_usage(data)
            else:
                usage = _ndjson_line_usage(line)  # Ollama NDJSON: bare JSON per line
            if usage is not None:
                found = usage
        return found
    return None


def _sse_chunk_usage(data: str) -> dict[str, int | None] | None:
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return None
    usage = chunk_usage(chunk) if isinstance(chunk, dict) else None
    return normalize_usage(usage) if usage is not None else None


def _ndjson_line_usage(line: str) -> dict[str, int | None] | None:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return ollama_usage(obj) if isinstance(obj, dict) else None


def total_tokens(usage: dict[str, int | None]) -> int | None:
    """The call's total tokens: the explicit `total` when the API reports it, else the
    sum of prompt + completion. `None` only when neither prompt nor completion is known."""
    if usage.get("total") is not None:
        return usage["total"]
    prompt, completion = usage.get("prompt"), usage.get("completion")
    if prompt is None and completion is None:
        return None
    return (prompt or 0) + (completion or 0)
