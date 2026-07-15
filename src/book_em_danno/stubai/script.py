"""The deterministic stub-AI: a step vocabulary, a script engine, and wire framing.

`danno bench`'s runaway gates and the exec watchdog need a model backend whose
behavior is *scripted*, not live — so gate behavior is reproducible without a GPU and
a runaway loop can be demonstrated on demand (see `.docs/plan-runaway-gates-validation.md`
and `.docs/plan-stub-ai-test-harness.md`). This module is the substrate the stub HTTP
server (`stubai.server`) drives:

- **Steps** (`ToolCall`, `Finish`, `ToolLoop`, `Drip`) — the scripted assistant moves.
  `ToolLoop` is the runaway reproducer: it answers *every* subsequent request with a
  tool call, forever (`n=None`) or `n` times.
- **`ScriptEngine`** — a thread-safe state machine that yields the next `Reply` per
  inbound inference request. Over-running the script settles to a `Finish`.
- **Wire framing** (`render`) — turns one `Reply` into the exact bytes a real harness
  would receive over each dialect it dials: OpenAI chat-completions (stream/non-stream,
  with/without `stream_options.include_usage`), Ollama-native `/api/chat` (JSON +
  NDJSON stream), the Responses API (SSE), and Anthropic Messages. The framing honors
  the request's own flags so the usage-less rows (F1: Ollama-native, SSE without usage)
  are wire-faithful, not synthesised.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# Deterministic default token counts stamped into every reply's `usage` block. Fixed so
# scripted Gate-2 (token) assertions are exact; steps do not vary them in GV0.
_PROMPT_TOKENS = 10
_COMPLETION_TOKENS = 5

# ---------------------------------------------------------------------------
# Step vocabulary — the scripted assistant moves (plan §3.2, GV0 subset).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One well-formed tool call, then the script advances to the next step."""

    name: str = "read_file"
    arguments: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Finish:
    """Final assistant text (`finish_reason: stop` / `done: true`)."""

    text: str = "done"


@dataclass(frozen=True)
class ToolLoop:
    """Answer *every* subsequent request with a tool call — the runaway reproducer.

    `n=None` loops forever; `n` bounds it to `n` tool calls, after which the script
    advances to the next step.
    """

    name: str = "read_file"
    arguments: Mapping[str, Any] = field(default_factory=dict)
    n: int | None = None


@dataclass(frozen=True)
class Drip:
    """Final assistant text, streamed slowly at `tokens_per_s` — latency injection
    (regression net for provider-stall-timeout bugs). Observable only on a streaming
    dialect; on a non-streaming request the delay collapses to nothing."""

    text: str = "done"
    tokens_per_s: float = 10.0


Step = ToolCall | Finish | ToolLoop | Drip


# ---------------------------------------------------------------------------
# Reply — the semantic content the framing renders, dialect-agnostic.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    """What the assistant says for one inference round, independent of wire dialect.

    A tool call sets `tool_name`; a text answer sets `text`. `drip_tokens_per_s`, when
    set, spreads the streamed text over time.
    """

    text: str = ""
    tool_name: str | None = None
    tool_arguments: Mapping[str, Any] = field(default_factory=dict)
    prompt_tokens: int = _PROMPT_TOKENS
    completion_tokens: int = _COMPLETION_TOKENS
    drip_tokens_per_s: float | None = None

    @property
    def is_tool_call(self) -> bool:
        return self.tool_name is not None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_DEFAULT_REPLY = Reply(text="done")


def _reply_for(step: Step) -> Reply:
    if isinstance(step, ToolCall):
        return Reply(tool_name=step.name, tool_arguments=step.arguments)
    if isinstance(step, ToolLoop):
        return Reply(tool_name=step.name, tool_arguments=step.arguments)
    if isinstance(step, Drip):
        return Reply(text=step.text, drip_tokens_per_s=step.tokens_per_s)
    return Reply(text=step.text)  # Finish


# ---------------------------------------------------------------------------
# ScriptEngine — one Reply per inbound inference request (thread-safe).
# ---------------------------------------------------------------------------


@dataclass
class ScriptEngine:
    """Pull the next `Reply` per inference request from a fixed list of steps.

    Thread-safe: the stub server answers on `ThreadingHTTPServer` worker threads. A
    `ToolLoop` is sticky — once reached it keeps returning tool calls without advancing
    (until `n` is exhausted). Requests past the end of the script settle to `Finish`
    (never an error), so an over-eager harness always gets a terminating answer.
    """

    steps: Sequence[Step]
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _pos: int = 0
    _loop: ToolLoop | None = None
    _loop_left: int | None = None

    def next_reply(self) -> Reply:
        with self._lock:
            while True:
                served = self._serve_active_loop()
                if served is not None:
                    return served
                if self._pos >= len(self.steps):
                    return _DEFAULT_REPLY  # over-run: keep settling to "done"
                step = self.steps[self._pos]
                self._pos += 1
                if isinstance(step, ToolLoop):
                    if step.n is not None and step.n <= 0:
                        continue  # a zero-length loop is a no-op; advance
                    self._loop = step
                    self._loop_left = None if step.n is None else step.n - 1
                    return _reply_for(step)
                return _reply_for(step)

    def _serve_active_loop(self) -> Reply | None:
        """A reply from the in-progress `ToolLoop`, or None if none is active/left."""
        if self._loop is None:
            return None
        if self._loop_left is None:  # forever
            return _reply_for(self._loop)
        if self._loop_left > 0:
            self._loop_left -= 1
            return _reply_for(self._loop)
        self._loop = None  # exhausted — fall through to the next step
        return None


# ---------------------------------------------------------------------------
# Wire framing — one Reply → the exact bytes of a chosen dialect.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WireResponse:
    """The rendered response: an ordered sequence of `(delay_before_s, data)` chunks and
    the `Content-Type`. `stream=True` means the server writes chunks incrementally and
    omits `Content-Length` (connection-close framing); `stream=False` is one buffered
    body with a `Content-Length`."""

    content_type: str
    chunks: tuple[tuple[float, bytes], ...]
    stream: bool


def dialect_for_path(path: str) -> str | None:
    """The wire dialect a request path implies, or None for a non-inference path.

    Path-based (not response-content-based) so it matches how a real harness picks an
    endpoint — the same discrimination the F1 fix moves the gate sensor onto."""
    base = path.split("?", 1)[0]
    if base.endswith("/chat/completions"):
        return "chat"
    if base.endswith("/responses"):
        return "responses"
    if base.endswith("/messages"):
        return "anthropic"
    if base.endswith("/api/chat") or base.endswith("/api/generate"):
        return "ollama"
    return None


def render(
    reply: Reply,
    *,
    dialect: str,
    stream: bool,
    include_usage: bool,
    model: str,
) -> WireResponse:
    """Render `reply` into `dialect`'s wire bytes, honoring the request's `stream` /
    `include_usage` flags (so the usage-less rows are produced, not faked)."""
    if dialect == "chat":
        return _render_chat(reply, stream=stream, include_usage=include_usage, model=model)
    if dialect == "ollama":
        return _render_ollama(reply, stream=stream, model=model)
    if dialect == "responses":
        return _render_responses(reply, model=model)
    if dialect == "anthropic":
        return _render_anthropic(reply, model=model)
    raise ValueError(f"unknown dialect: {dialect!r}")


def _text_tokens(text: str) -> list[str]:
    """Split text into stream deltas whose concatenation is the original text. Empty
    text yields a single empty delta so a streamed reply always emits one content chunk."""
    tokens = re.findall(r"\S+\s*|\s+", text)
    return tokens or [""]


def _tool_call_arguments(reply: Reply) -> str:
    return json.dumps(dict(reply.tool_arguments))


def _sse(event: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(event)}\n\n".encode()


def _sse_done() -> bytes:
    return b"data: [DONE]\n\n"


# --- chat-completions (OpenAI-compatible) ----------------------------------


def _render_chat(reply: Reply, *, stream: bool, include_usage: bool, model: str) -> WireResponse:
    if not stream:
        return _buffered_json(_chat_body(reply, model))
    return _chat_sse(reply, include_usage=include_usage, model=model)


def _chat_body(reply: Reply, model: str) -> dict[str, Any]:
    if reply.is_tool_call:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_stub_1",
                    "type": "function",
                    "function": {"name": reply.tool_name, "arguments": _tool_call_arguments(reply)},
                }
            ],
        }
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": reply.text}
        finish = "stop"
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens,
            "total_tokens": reply.total_tokens,
        },
    }


def _chat_sse(reply: Reply, *, include_usage: bool, model: str) -> WireResponse:
    head = {"id": "chatcmpl-stub", "object": "chat.completion.chunk", "model": model}
    chunks: list[tuple[float, bytes]] = [
        (0.0, _sse({**head, "choices": [{"index": 0, "delta": {"role": "assistant"}}]}))
    ]
    if reply.is_tool_call:
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call_stub_1",
                    "type": "function",
                    "function": {"name": reply.tool_name, "arguments": _tool_call_arguments(reply)},
                }
            ]
        }
        chunks.append((0.0, _sse({**head, "choices": [{"index": 0, "delta": delta}]})))
        finish = "tool_calls"
    else:
        delay = _drip_delay(reply)
        for token in _text_tokens(reply.text):
            chunks.append(
                (delay, _sse({**head, "choices": [{"index": 0, "delta": {"content": token}}]}))
            )
        finish = "stop"
    chunks.append(
        (0.0, _sse({**head, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}))
    )
    if include_usage:
        usage = {
            "prompt_tokens": reply.prompt_tokens,
            "completion_tokens": reply.completion_tokens,
            "total_tokens": reply.total_tokens,
        }
        chunks.append((0.0, _sse({**head, "choices": [], "usage": usage})))
    chunks.append((0.0, _sse_done()))
    return WireResponse("text/event-stream", tuple(chunks), stream=True)


# --- Ollama native /api/chat -----------------------------------------------


def _render_ollama(reply: Reply, *, stream: bool, model: str) -> WireResponse:
    if not stream:
        return _buffered_json(_ollama_final(reply, model, streaming=False))
    return _ollama_ndjson(reply, model=model)


def _ollama_message(reply: Reply) -> dict[str, Any]:
    if reply.is_tool_call:
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": reply.tool_name, "arguments": dict(reply.tool_arguments)}}
            ],
        }
    return {"role": "assistant", "content": reply.text}


def _ollama_final(reply: Reply, model: str, *, streaming: bool) -> dict[str, Any]:
    """The terminal `done: true` object. Carries `prompt_eval_count`/`eval_count` — Ollama
    native has NO `usage` block, which is exactly why the current gate sensor is blind to
    it (F1). In the NDJSON stream form the message content is empty (already dripped)."""
    message = {"role": "assistant", "content": ""} if streaming else _ollama_message(reply)
    body: dict[str, Any] = {
        "model": model,
        "message": message,
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": reply.prompt_tokens,
        "eval_count": reply.completion_tokens,
    }
    if streaming and reply.is_tool_call:
        body["message"] = _ollama_message(reply)
    return body


def _ollama_ndjson(reply: Reply, *, model: str) -> WireResponse:
    chunks: list[tuple[float, bytes]] = []
    if reply.is_tool_call:
        chunks.append(
            (0.0, _ndjson({"model": model, "message": _ollama_message(reply), "done": False}))
        )
    else:
        delay = _drip_delay(reply)
        for token in _text_tokens(reply.text):
            line = {
                "model": model,
                "message": {"role": "assistant", "content": token},
                "done": False,
            }
            chunks.append((delay, _ndjson(line)))
    chunks.append((0.0, _ndjson(_ollama_final(reply, model, streaming=True))))
    return WireResponse("application/x-ndjson", tuple(chunks), stream=True)


def _ndjson(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj) + "\n").encode()


# --- Responses API (SSE) ----------------------------------------------------


def _render_responses(reply: Reply, *, model: str) -> WireResponse:
    """opencode ↔ NVIDIA via `@ai-sdk/openai` streams the Responses API; the terminal
    `response.completed` event carries `response.usage` (input/output tokens)."""
    chunks: list[tuple[float, bytes]] = []
    if reply.is_tool_call:
        output: list[dict[str, Any]] = [
            {
                "type": "function_call",
                "name": reply.tool_name,
                "arguments": _tool_call_arguments(reply),
            }
        ]
    else:
        delay = _drip_delay(reply)
        for token in _text_tokens(reply.text):
            chunks.append((delay, _sse({"type": "response.output_text.delta", "delta": token})))
        output = [{"type": "message", "content": [{"type": "output_text", "text": reply.text}]}]
    completed = {
        "type": "response.completed",
        "response": {
            "output": output,
            "usage": {
                "input_tokens": reply.prompt_tokens,
                "output_tokens": reply.completion_tokens,
                "total_tokens": reply.total_tokens,
            },
        },
    }
    chunks.append((0.0, _sse(completed)))
    chunks.append((0.0, _sse_done()))
    return WireResponse("text/event-stream", tuple(chunks), stream=True)


# --- Anthropic Messages (non-stream) ---------------------------------------


def _render_anthropic(reply: Reply, *, model: str) -> WireResponse:
    if reply.is_tool_call:
        content: list[dict[str, Any]] = [
            {
                "type": "tool_use",
                "id": "toolu_stub_1",
                "name": reply.tool_name,
                "input": dict(reply.tool_arguments),
            }
        ]
        stop = "tool_use"
    else:
        content = [{"type": "text", "text": reply.text}]
        stop = "end_turn"
    body = {
        "id": "msg_stub",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "usage": {"input_tokens": reply.prompt_tokens, "output_tokens": reply.completion_tokens},
    }
    return _buffered_json(body)


# --- shared helpers ---------------------------------------------------------


def _buffered_json(body: dict[str, Any]) -> WireResponse:
    return WireResponse("application/json", ((0.0, json.dumps(body).encode()),), stream=False)


def _drip_delay(reply: Reply) -> float:
    if reply.drip_tokens_per_s and reply.drip_tokens_per_s > 0:
        return 1.0 / reply.drip_tokens_per_s
    return 0.0
