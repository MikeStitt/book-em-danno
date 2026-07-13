"""Derive per-request metrics from a permutation's `--capture` wire JSONL (§1, §2.2/2.3,
§6). Pure post-processing: the recording proxy already stamped each request/response
with `ts` and the model's `/v1` response body carries `usage`, so token split, cached
tokens, per-request round-trip time, tokens/sec, and the context-occupancy curve fall
straight out of the recording — no new infrastructure and harness-agnostic (so occ, whose
`stream-json` reports `tokens==0`, still yields real totals from the wire, §1.3).

`render_transcript` turns the same (already-redacted) JSONL into a readable markdown
dump (§3.4); it inherits the proxy's secret redaction for free.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# The local Ollama/NVIDIA path runs non-streaming (`CLAUDE_CODE_STREAMING=0`) and the
# proxy buffers the whole response, so there is a single response timestamp per call:
# "time to first token" is really the whole-response time. Labelled so it is never
# mistaken for true prefill latency (§2.2).
TTFT_LABEL = "whole-response (non-streaming)"


@dataclass(frozen=True)
class RequestMetric:
    """One `/v1` model call within a turn."""

    seq: int
    path: str
    rtt_s: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    total_tokens: int | None
    tok_per_s: float | None


@dataclass(frozen=True)
class TurnWireMetrics:
    """A turn's derived wire metrics: scalar rollups + the per-request series."""

    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    tok_per_s: float | None = None
    ttft_s: float | None = None
    ttft_label: str = TTFT_LABEL
    rtt_min_s: float | None = None
    rtt_max_s: float | None = None
    rtt_mean_s: float | None = None
    peak_ctx_tokens: int | None = None
    ctx_growth: list[int] = field(default_factory=list)  # §6.2 prompt_tokens per call, in order
    ctx_deltas: list[int] = field(default_factory=list)  # §1.4 per-round context deltas
    ctx_headroom_pct: float | None = None  # §6.3 (filled by the reporter given num_ctx)
    requests: list[RequestMetric] = field(default_factory=list)


def _normalize_usage(usage: dict) -> dict[str, int | None]:
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


def _chunk_usage(chunk: dict) -> dict | None:
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


def _extract_usage(body: Any) -> dict[str, int | None] | None:
    """Pull normalized `usage` from a response body — a parsed JSON object (non-stream,
    both chat-completions and Responses carry `usage` at the top level) or an SSE text
    blob (`data: {…}` lines; the last chunk carrying `usage` wins)."""
    if isinstance(body, dict):
        usage = body.get("usage")
        return _normalize_usage(usage) if isinstance(usage, dict) else None
    if isinstance(body, str):
        found: dict[str, int | None] | None = None
        for line in body.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if not data or data == "[DONE]":
                continue
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage = _chunk_usage(chunk) if isinstance(chunk, dict) else None
            if usage is not None:
                found = _normalize_usage(usage)
        return found
    return None


def parse_capture_records(records: list[dict]) -> list[RequestMetric]:
    """Pair request/response records by `seq` and derive one `RequestMetric` per model
    call. Records without an extractable `usage` (e.g. `/api/tags`, `/models`) are
    skipped — only inference calls become metrics."""
    requests = {r["seq"]: r for r in records if r.get("direction") == "request"}
    responses = {r["seq"]: r for r in records if r.get("direction") == "response"}
    metrics: list[RequestMetric] = []
    for seq in sorted(requests):
        resp = responses.get(seq)
        if resp is None:
            continue
        usage = _extract_usage(resp.get("body"))
        if usage is None:
            continue
        req = requests[seq]
        rtt = _rtt(req.get("ts"), resp.get("ts"))
        completion = usage["completion"]
        tok_per_s = (
            round(completion / rtt, 1)
            if completion is not None and rtt is not None and rtt > 0
            else None
        )
        metrics.append(
            RequestMetric(
                seq=seq,
                path=req.get("path", ""),
                rtt_s=rtt,
                prompt_tokens=usage["prompt"],
                completion_tokens=completion,
                cached_tokens=usage["cached"],
                total_tokens=usage["total"],
                tok_per_s=tok_per_s,
            )
        )
    return metrics


def _rtt(req_ts: Any, resp_ts: Any) -> float | None:
    if isinstance(req_ts, int | float) and isinstance(resp_ts, int | float):
        return round(max(0.0, resp_ts - req_ts), 3)
    return None


def rollup(metrics: list[RequestMetric]) -> TurnWireMetrics:
    """Reduce per-request metrics to a turn's scalar rollups + growth series."""
    if not metrics:
        return TurnWireMetrics()
    prompts = [m.prompt_tokens for m in metrics if m.prompt_tokens is not None]
    completions = [m.completion_tokens or 0 for m in metrics]
    cached = [m.cached_tokens or 0 for m in metrics]
    totals = [m.total_tokens or 0 for m in metrics]
    rtts = [m.rtt_s for m in metrics if m.rtt_s is not None]
    gen_time = sum(m.rtt_s for m in metrics if m.rtt_s and m.completion_tokens)
    output = sum(completions)
    ctx_growth = list(prompts)
    return TurnWireMetrics(
        request_count=len(metrics),
        input_tokens=sum(prompts),
        output_tokens=output,
        cached_tokens=sum(cached),
        total_tokens=sum(totals),
        tok_per_s=round(output / gen_time, 1) if gen_time > 0 else None,
        ttft_s=metrics[0].rtt_s,  # first call's whole-response time (labelled)
        rtt_min_s=min(rtts) if rtts else None,
        rtt_max_s=max(rtts) if rtts else None,
        rtt_mean_s=round(sum(rtts) / len(rtts), 3) if rtts else None,
        peak_ctx_tokens=max(prompts) if prompts else None,
        ctx_growth=ctx_growth,
        ctx_deltas=[b - a for a, b in zip(ctx_growth, ctx_growth[1:], strict=False)],
        requests=metrics,
    )


def headroom_pct(peak_ctx_tokens: int | None, num_ctx: int | None) -> float | None:
    """Percent of the model's loaded context window still free at peak occupancy (§6.3).
    Compared against the model's real `/api/show` ceiling (`num_ctx`), NOT opencode's
    client-side `context_budget`. `None` when either input is missing/zero."""
    if not peak_ctx_tokens or not num_ctx:
        return None
    return round(100.0 * (1.0 - peak_ctx_tokens / num_ctx), 1)


def metrics_from_files(paths: list[Path]) -> TurnWireMetrics:
    """Read + parse every capture file for a permutation (usually one backend) and roll
    up. Records are paired per file, then the per-request metrics are combined in order."""
    from book_em_danno.capture.proxy import read_captures

    all_metrics: list[RequestMetric] = []
    for path in paths:
        all_metrics.extend(parse_capture_records(read_captures(path)))
    all_metrics.sort(key=lambda m: m.seq)
    return rollup(all_metrics)


def write_metrics(path: Path, metrics: TurnWireMetrics) -> Path:
    """Write a permutation's derived metrics to `metrics/<perm>.json`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(metrics), indent=2) + "\n", encoding="utf-8")
    return path


# --- §3.4 readable transcript -------------------------------------------------


def render_transcript(records: list[dict]) -> str:
    """Render capture records into a readable markdown transcript (system prompt →
    each round's messages/tool-calls → completion + usage). Reads the already-redacted
    JSONL, so no secret can re-surface."""
    requests = {r["seq"]: r for r in records if r.get("direction") == "request"}
    responses = {r["seq"]: r for r in records if r.get("direction") == "response"}
    lines: list[str] = ["# Wire transcript", ""]
    for seq in sorted(requests):
        req = requests[seq]
        body = req.get("body")
        lines.append(f"## Call {seq} — `{req.get('method', '?')} {req.get('path', '')}`")
        lines.append("")
        if isinstance(body, dict):
            if body.get("model"):
                lines.append(f"- model: `{body['model']}`")
            tools = body.get("tools")
            if isinstance(tools, list):
                lines.append(f"- tools offered: {len(tools)}")
            lines.append("")
            # `instructions` carries the Responses-API system prompt separately from the
            # turn messages; chat-completions folds it into `messages` as a system role.
            if isinstance(body.get("instructions"), str):
                lines.extend(_render_message({"role": "system", "content": body["instructions"]}))
            # chat-completions uses `messages`; the Responses API uses `input`.
            for msg in body.get("messages") or body.get("input") or []:
                lines.extend(_render_message(msg))
        resp = responses.get(seq)
        if resp is not None:
            lines.extend(_render_response(resp))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_message(msg: dict) -> list[str]:
    role = msg.get("role", "?")
    text = _content_text(msg.get("content"))
    return [f"**{role}:**", "", "```", text, "```", ""]


def _content_text(content: Any) -> str:
    """Flatten a message's `content` to text. Chat-completions uses a plain string; the
    Responses API uses a list of typed blocks (`{type: "input_text"/"output_text", text}`).
    Blocks without a `text` field fall back to their JSON so nothing is silently dropped."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block["text"]
            if isinstance(block, dict) and isinstance(block.get("text"), str)
            else json.dumps(block, ensure_ascii=False)
            for block in content
        ]
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


def _render_response(resp: dict) -> list[str]:
    body = resp.get("body")
    out = [f"**response** (status {resp.get('status', '?')}):", ""]
    usage = _extract_usage(body)
    if isinstance(body, dict):
        out.extend(_render_chat_output(body))
    elif isinstance(body, str):
        out.extend(_render_responses_output(body))
    if usage is not None:
        out.append(
            f"- usage: prompt={usage['prompt']} completion={usage['completion']} "
            f"cached={usage['cached']}"
        )
    out.append("")
    return out


def _render_chat_output(body: dict) -> list[str]:
    """Render a chat-completions response body's `choices[].message` content + tool calls."""
    out: list[str] = []
    for choice in body.get("choices") or []:
        message = choice.get("message") or {}
        if message.get("content"):
            out += ["```", str(message["content"]), "```", ""]
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            out.append(f"- tool_call: `{fn.get('name', '?')}({fn.get('arguments', '')})`")
    return out


def _render_responses_output(sse: str) -> list[str]:
    """Render a Responses-API SSE stream's final `output[]` (from `response.completed`):
    assistant `message` text and `function_call` items. Without this the transcript's
    response section is empty, since the Responses body is an SSE string, not a dict."""
    output = _final_responses_output(sse)
    if output is None:
        return []
    out: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "function_call":
            out.append(f"- tool_call: `{item.get('name', '?')}({item.get('arguments', '')})`")
        elif kind == "message":
            text = _content_text(item.get("content"))
            if text:
                out += ["```", text, "```", ""]
    return out


def _final_responses_output(sse: str) -> list | None:
    """The `response.output` array from the last completed/incomplete Responses SSE event."""
    output: list | None = None
    for line in sse.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict) or chunk.get("type") not in (
            "response.completed",
            "response.incomplete",
        ):
            continue
        resp = chunk.get("response")
        if isinstance(resp, dict) and isinstance(resp.get("output"), list):
            output = resp["output"]
    return output


def write_transcript(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_transcript(records), encoding="utf-8")
    return path
