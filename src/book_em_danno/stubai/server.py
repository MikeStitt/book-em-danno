"""The stub-AI HTTP server: a *terminating* model backend driven by a script.

Same skeleton as `capture.proxy` (`ThreadingHTTPServer` + a JSONL transcript in the
identical schema, so `telemetry.wire_metrics` reads stub transcripts and live captures
interchangeably) but it answers from a `ScriptEngine` instead of forwarding upstream. It
sits exactly where host Ollama sits today — a validation harness points the capture
proxy's `upstream` at it (Tier A) or wires a sandboxed harness at it (Tier B), and every
existing plumbing path is reused unchanged (`.docs/plan-stub-ai-test-harness.md` §3).

Inference POSTs (`/v1/chat/completions`, `/api/chat`, `/v1/responses`, `/v1/messages`,
`/api/generate`) consume one script step and are rendered in that path's dialect. Discovery
GETs (`/api/tags`, `/v1/models`, `/api/version`, `/api/show`) answer from a static table
**without** consuming a step, so scripted round counts stay exact. Anything else is a
recorded 404 — a self-documenting gap for the first unknown dialect a harness dials.
"""

from __future__ import annotations

import itertools
import json
import socketserver
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from book_em_danno.core.exec import CommandFailedError
from book_em_danno.stubai.script import (
    ScriptEngine,
    Step,
    WireResponse,
    dialect_for_path,
    render,
)

# Discovery/health responses served from a static table (no script step consumed). Keyed
# by the path suffix a harness dials at startup or for title-gen model lookup.
_DISCOVERY: dict[str, dict[str, Any]] = {
    "/api/tags": {"models": [{"name": "stub", "model": "stub"}]},
    "/api/version": {"version": "0.0.0-stub"},
    "/v1/models": {"object": "list", "data": [{"id": "stub", "object": "model"}]},
    "/api/show": {"model_info": {}, "capabilities": ["completion", "tools"]},
    "/api/ps": {"models": []},
}


@dataclass(frozen=True)
class StubConfig:
    """One stub instance: bind `port` (0 = ephemeral) on 0.0.0.0, answer from `script`,
    record every exchange to `transcript_file` in the `capture.proxy` JSONL schema."""

    script: list[Step]
    transcript_file: Path
    port: int = 0


class StubServer(ThreadingHTTPServer):
    """Threading HTTP server carrying the script engine, a seq counter, and a write lock
    so concurrent worker threads append to the transcript atomically."""

    def __init__(self, cfg: StubConfig) -> None:
        self.cfg = cfg
        self.engine = ScriptEngine(cfg.script)
        self.counter = itertools.count(1)
        self.write_lock = threading.Lock()
        super().__init__(("0.0.0.0", cfg.port), _Handler)

    def server_bind(self) -> None:
        # Bypass HTTPServer.server_bind's socket.getfqdn(host) reverse-DNS lookup,
        # which hangs for tens of seconds on hosts without reverse DNS for their
        # own name (notably macOS CI runners) — we never read server_name.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = cast("str", host)
        self.server_port = port


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 default → each response is delimited by connection close, so a streamed
    # (Content-Length-less) SSE/NDJSON body is read correctly by urllib and the proxy.

    def log_message(self, *_args: Any) -> None:
        pass

    @property
    def _srv(self) -> StubServer:
        return cast("StubServer", self.server)

    def _record(self, record: dict[str, Any]) -> None:
        line = json.dumps(record) + "\n"
        with self._srv.write_lock, self._srv.cfg.transcript_file.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        self._handle(self._read_body())

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._handle(b"")

    def _handle(self, raw: bytes) -> None:
        seq = next(self._srv.counter)
        body = _decode_json(raw)
        self._record(
            {
                "seq": seq,
                "direction": "request",
                "ts": time.time(),
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body if body is not None else _decode_text(raw),
            }
        )
        dialect = dialect_for_path(self.path)
        if self.command == "POST" and dialect is not None:
            self._answer_inference(seq, dialect, body if isinstance(body, dict) else {})
            return
        static = _DISCOVERY.get(self.path.split("?", 1)[0])
        if static is not None:
            self._answer_static(seq, static)
            return
        self._answer_404(seq)

    def _answer_inference(self, seq: int, dialect: str, body: dict[str, Any]) -> None:
        reply = self._srv.engine.next_reply()
        wire = render(
            reply,
            dialect=dialect,
            stream=_stream_flag(body, dialect),
            include_usage=_include_usage(body),
            model=str(body.get("model", "stub")),
        )
        self._send_wire(seq, wire)

    def _answer_static(self, seq: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self._send_buffered(seq, 200, "application/json", data)

    def _answer_404(self, seq: int) -> None:
        data = json.dumps({"error": f"stub-ai: no route for {self.command} {self.path}"}).encode()
        self._send_buffered(seq, 404, "application/json", data)

    def _send_wire(self, seq: int, wire: WireResponse) -> None:
        assembled = bytearray()
        self.send_response(200)
        self.send_header("Content-Type", wire.content_type)
        if not wire.stream:
            total = sum(len(data) for _, data in wire.chunks)
            self.send_header("Content-Length", str(total))
        self.end_headers()
        for delay, data in wire.chunks:
            if delay > 0:
                time.sleep(delay)
            self.wfile.write(data)
            self.wfile.flush()
            assembled += data
        self._record_response(seq, 200, wire.content_type, bytes(assembled))

    def _send_buffered(self, seq: int, status: int, content_type: str, data: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self._record_response(seq, status, content_type, data)

    def _record_response(self, seq: int, status: int, content_type: str, data: bytes) -> None:
        self._record(
            {
                "seq": seq,
                "direction": "response",
                "ts": time.time(),
                "status": status,
                "headers": {"Content-Type": content_type},
                "body": _decode_json(data) if _is_json(content_type) else _decode_text(data),
            }
        )


@dataclass
class Stub:
    """A running stub's handle: its address plus transcript accessors (plan §3.3)."""

    port: int
    transcript_file: Path
    _server: StubServer = field(repr=False)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def records(self) -> list[dict[str, Any]]:
        """Every request/response record so far, in the `capture.proxy` JSONL schema."""
        if not self.transcript_file.is_file():
            return []
        text = self.transcript_file.read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line]

    def completion_requests(self, model: str | None = None) -> int:
        """Count of inference round-trips (POSTs to an inference endpoint), optionally
        filtered to `model`. Discovery hits are excluded — the scripted round count."""
        count = 0
        for rec in self.records():
            if rec.get("direction") != "request" or rec.get("method") != "POST":
                continue
            if dialect_for_path(rec.get("path", "")) is None:
                continue
            if model is not None and _record_model(rec) != model:
                continue
            count += 1
        return count


@contextmanager
def stub_ai(cfg: StubConfig) -> Iterator[Stub]:
    """Run the stub AI on `0.0.0.0:<port>` (ephemeral when `port=0`) for the block.

    Binds 0.0.0.0 so a sandbox VM can reach it via `host.docker.internal`. Truncates the
    transcript on entry. Fails loud (Working Rule 8) if a fixed port is already taken."""
    cfg.transcript_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.transcript_file.write_text("", encoding="utf-8")
    try:
        server = StubServer(cfg)
    except OSError as exc:
        raise CommandFailedError(
            f"stub-ai could not bind 0.0.0.0:{cfg.port} ({exc}); is the port in use?"
        ) from exc
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield Stub(
            port=server.server_address[1], transcript_file=cfg.transcript_file, _server=server
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _stream_flag(body: dict[str, Any], dialect: str) -> bool:
    """Whether to stream. Honors an explicit `stream` field; else uses each dialect's
    real default (Ollama `/api/chat` and the Responses API stream by default; OpenAI
    chat-completions and Anthropic do not)."""
    if "stream" in body:
        return bool(body["stream"])
    return dialect in ("ollama", "responses")


def _include_usage(body: dict[str, Any]) -> bool:
    opts = body.get("stream_options")
    return bool(isinstance(opts, dict) and opts.get("include_usage"))


def _record_model(rec: dict[str, Any]) -> Any:
    body = rec.get("body")
    return body.get("model") if isinstance(body, dict) else None


def _decode_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _decode_text(raw: bytes) -> str | None:
    if not raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_json(content_type: str) -> bool:
    return content_type.startswith("application/json")
