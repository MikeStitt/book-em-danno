"""A recording-and-re-originating HTTP proxy for `danno --capture`.

The sandboxed agent dials this proxy (via a backend `base_url` rewritten by
`capture.wiring`); the proxy records each REQUEST and the upstream RESPONSE as JSONL,
then forwards to the real backend — plain HTTP for local Ollama, HTTPS for cloud
(NVIDIA NIM) since `urllib` re-originates TLS for an `https://` upstream. Inbound
auth headers are passed through so cloud backends authenticate; their VALUES are
redacted in the capture so live tokens never land on disk.

The upstream response is buffered fully before replay (as the slow-test proxy does):
opencode parses a buffered SSE body fine, and buffering is what lets us record it.

Promoted/generalized from the slow-test helper `tests/slow/capture_proxy.py` (which
stays as-is for the live wire-contract test). This module is the package feature: it
adds configurable upstream, header passthrough, request+response capture, and secret
redaction.
"""

from __future__ import annotations

import base64
import itertools
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from book_em_danno.capture.gate import GateTally
from book_em_danno.capture.usage import extract_usage, total_tokens
from book_em_danno.core.exec import CommandFailedError

# Header names whose VALUES carry secrets — recorded as "<redacted>", never verbatim.
_REDACT = frozenset({"authorization", "x-api-key", "api-key"})
# Headers we must NOT forward upstream verbatim: Host/Content-Length are recomputed by
# urllib from the URL/data; Connection is hop-by-hop; Accept-Encoding is forced to
# identity below so captured response bodies aren't gzipped.
_DROP_UPSTREAM = frozenset({"host", "content-length", "connection", "accept-encoding"})
# A cold local model load + full generation can take minutes; be generous.
_UPSTREAM_TIMEOUT_S = 600


@dataclass(frozen=True)
class CaptureProxyConfig:
    """One proxy instance: bind `port` on 0.0.0.0, re-originate to `upstream`.

    `upstream` is scheme+host[:port] only (no path) — the request path is appended
    verbatim, so `https://integrate.api.nvidia.com` + `/v1/chat/completions` works.
    `pass_headers` forwards inbound headers (auth included) upstream; turn it off for
    a no-auth local backend if a minimal request is wanted.
    """

    upstream: str
    capture_file: Path
    port: int
    pass_headers: bool = True
    # When set (`danno bench`'s runaway gates), each usage-bearing response feeds this live
    # tally so the exec watchdog can trip Gate 1 (round count) / Gate 2 (tokens) mid-cell.
    tally: GateTally | None = None


def _redact_headers(headers: Any) -> dict[str, str]:
    return {k: ("<redacted>" if k.lower() in _REDACT else v) for k, v in headers.items()}


def _decode_body(raw: bytes) -> Any:
    """Parsed JSON when possible (the common case), else UTF-8 text (SSE), else a
    base64 wrapper for binary — so a record is always JSON-serialisable and readable."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"_b64": base64.b64encode(raw).decode("ascii")}


class _CaptureServer(ThreadingHTTPServer):
    """Threading HTTP server carrying the proxy config, a request-id counter, and a
    lock so concurrent handler threads append to the capture file atomically."""

    def __init__(self, cfg: CaptureProxyConfig) -> None:
        self.cfg = cfg
        self.counter = itertools.count(1)
        self.write_lock = threading.Lock()
        super().__init__(("0.0.0.0", cfg.port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args: Any) -> None:  # noqa: D401 - silence default logging
        pass

    @property
    def _server(self) -> _CaptureServer:
        return cast("_CaptureServer", self.server)

    def _record(self, record: dict[str, Any]) -> None:
        server = self._server
        line = json.dumps(record) + "\n"
        with server.write_lock, server.cfg.capture_file.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def _proxy(self, body: bytes | None) -> None:
        cfg = self._server.cfg
        seq = next(self._server.counter)
        self._record(
            {
                "seq": seq,
                "direction": "request",
                "ts": time.time(),  # epoch seconds; response-request delta = per-call RTT (§2.3)
                "method": self.command,
                "path": self.path,
                "headers": _redact_headers(self.headers),
                "body": _decode_body(body or b""),
            }
        )

        if cfg.pass_headers:
            headers = {k: v for k, v in self.headers.items() if k.lower() not in _DROP_UPSTREAM}
            headers["Accept-Encoding"] = "identity"
        else:
            headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        req = urllib.request.Request(
            f"{cfg.upstream}{self.path}", data=body, method=self.command, headers=headers
        )
        try:
            with urllib.request.urlopen(req, timeout=_UPSTREAM_TIMEOUT_S) as resp:
                status, resp_headers, payload = resp.status, dict(resp.headers.items()), resp.read()
        except urllib.error.HTTPError as exc:  # upstream non-2xx — still capture + replay it
            status = exc.code
            resp_headers = dict(exc.headers.items()) if exc.headers else {}
            payload = exc.read()
        except urllib.error.URLError as exc:  # upstream unreachable — synthesise a 502
            status, resp_headers = 502, {"Content-Type": "application/json"}
            payload = json.dumps({"error": f"capture proxy upstream error: {exc.reason}"}).encode()

        resp_body = _decode_body(payload)
        self._record(
            {
                "seq": seq,
                "direction": "response",
                "ts": time.time(),  # buffered-response completion time (§2.3 RTT / §2.2 TTFT)
                "status": status,
                "headers": _redact_headers(resp_headers),
                "body": resp_body,
            }
        )
        if cfg.tally is not None:
            # Only usage-bearing responses are inference calls; discovery hits (no usage)
            # are not counted — keeps Gate 1 aligned with `parse_capture_records`.
            usage = extract_usage(resp_body)
            if usage is not None:
                cfg.tally.record(tokens=total_tokens(usage))

        self.send_response(status)
        self.send_header("Content-Type", resp_headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        self._proxy(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server naming
        self._proxy(None)


@contextmanager
def capture_proxy(cfg: CaptureProxyConfig) -> Iterator[CaptureProxyConfig]:
    """Run the recording proxy on `0.0.0.0:<cfg.port>` for the duration of the block.

    Binds 0.0.0.0 so the sandbox VM can reach it via `host.docker.internal`. Fails
    loud (Working Rule 8) if the port is already taken. Truncates the capture file on
    entry; read records back with `read_captures`."""
    cfg.capture_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.capture_file.write_text("", encoding="utf-8")
    try:
        server = _CaptureServer(cfg)
    except OSError as exc:
        raise CommandFailedError(
            f"capture proxy could not bind 0.0.0.0:{cfg.port} ({exc}); is the port in use?"
        ) from exc
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield cfg
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def read_captures(path: Path) -> list[dict[str, Any]]:
    """Parse a capture file into its list of `{seq, direction, …}` records."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
