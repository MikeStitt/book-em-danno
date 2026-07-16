"""Unit tests for GateTally and the capture proxy feeding it live.

The proxy test drives a real proxy against a stub upstream (no Docker): a POST to an
inference endpoint advances the tally, a discovery `GET` does not — matching Gate 1's
"inference rounds, not discovery hits" rule, which the proxy decides by request path
(`is_inference_request`), not by whether a `usage` block came back. The usage-less-dialect
coverage lives in `test_gate_sensor_dialects` (the F1 net).
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from book_em_danno.capture.gate import GateTally
from book_em_danno.capture.proxy import CaptureProxyConfig, capture_proxy


def test_gate_tally_counts_calls_and_sums_tokens() -> None:
    tally = GateTally()
    assert (tally.inference_calls(), tally.tokens()) == (0, 0)
    tally.record(tokens=100)
    tally.record(tokens=None)  # a round with no token info still counts as a call
    tally.record(tokens=50)
    assert tally.inference_calls() == 3
    assert tally.tokens() == 150


class _Upstream(BaseHTTPRequestHandler):
    """POST /v1/chat/completions → a usage-bearing body; GET → a discovery body (no usage)."""

    def log_message(self, *_a: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        self._send(
            json.dumps(
                {
                    "choices": [{"message": {"content": "hi"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            ).encode()
        )

    def do_GET(self) -> None:  # noqa: N802
        self._send(json.dumps({"models": []}).encode())  # /api/tags-like: no usage

    def _send(self, payload: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def _upstream() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def test_proxy_feeds_tally_on_inference_posts_not_discovery(tmp_path: Path) -> None:
    tally = GateTally()
    with _upstream() as up_port:
        cfg = CaptureProxyConfig(
            upstream=f"http://127.0.0.1:{up_port}",
            capture_file=tmp_path / "cap.jsonl",
            port=_free_port(),
            tally=tally,
        )
        with capture_proxy(cfg):
            for _ in range(2):  # two inference calls (carry usage)
                req = urllib.request.Request(
                    f"http://127.0.0.1:{cfg.port}/v1/chat/completions",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=10).read()
            # one discovery call (GET /api/tags) — must NOT advance the tally
            urllib.request.urlopen(f"http://127.0.0.1:{cfg.port}/api/tags", timeout=10).read()

    assert tally.inference_calls() == 2
    assert tally.tokens() == 30  # 2 × 15
    assert not tally.blind()  # saw inference rounds, so not a blind cell
