"""Unit tests for the recording-and-re-originating capture proxy.

Drives the proxy against a stub upstream HTTP server (no Docker): asserts it records
the request AND response, passes auth headers upstream while redacting them in the
capture, decodes bodies sensibly, and fails loud on a busy port.
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

import pytest

from book_em_danno.capture.proxy import (
    CaptureProxyConfig,
    _decode_body,
    capture_proxy,
    read_captures,
)
from book_em_danno.core.exec import CommandFailedError


class _Upstream(BaseHTTPRequestHandler):
    received: list[dict[str, object]] = []

    def log_message(self, *_a: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        type(self).received.append(
            {"path": self.path, "headers": dict(self.headers.items()), "body": body}
        )
        payload = json.dumps({"ok": True, "echo": json.loads(body)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@contextmanager
def _upstream() -> Iterator[int]:
    _Upstream.received = []
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


def _lower(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def test_records_request_and_response_passes_auth_redacts_capture(tmp_path: Path) -> None:
    with _upstream() as up_port:
        cfg = CaptureProxyConfig(
            upstream=f"http://127.0.0.1:{up_port}",
            capture_file=tmp_path / "cap.jsonl",
            port=_free_port(),
        )
        with capture_proxy(cfg):
            req = urllib.request.Request(
                f"http://127.0.0.1:{cfg.port}/v1/chat/completions",
                data=json.dumps({"model": "m", "messages": []}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer SECRET",
                    "x-api-key": "KEYVAL",
                },
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=10).read())

        assert resp["ok"] is True  # client got the upstream response back

        # Upstream received the REAL auth header (passthrough so cloud authenticates).
        up_headers = _lower(_Upstream.received[-1]["headers"])  # type: ignore[arg-type]
        assert up_headers.get("authorization") == "Bearer SECRET"
        assert up_headers.get("x-api-key") == "KEYVAL"

        recs = read_captures(cfg.capture_file)
        request = next(r for r in recs if r["direction"] == "request")
        response = next(r for r in recs if r["direction"] == "response")
        assert request["path"] == "/v1/chat/completions"
        assert request["body"] == {"model": "m", "messages": []}
        # Secrets are redacted in the capture (never written verbatim to disk).
        assert _lower(request["headers"])["authorization"] == "<redacted>"
        assert _lower(request["headers"])["x-api-key"] == "<redacted>"
        assert "SECRET" not in json.dumps(request["headers"])
        assert "KEYVAL" not in json.dumps(request["headers"])
        assert response["status"] == 200
        assert response["body"]["ok"] is True
        assert request["seq"] == response["seq"]  # paired by sequence id


def test_busy_port_fails_loud(tmp_path: Path) -> None:
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    holder.bind(("0.0.0.0", 0))
    holder.listen()
    port = holder.getsockname()[1]
    try:
        with (
            pytest.raises(CommandFailedError),
            capture_proxy(
                CaptureProxyConfig(
                    upstream="http://127.0.0.1:1", capture_file=tmp_path / "c.jsonl", port=port
                )
            ),
        ):
            pass
    finally:
        holder.close()


def test_decode_body_variants() -> None:
    assert _decode_body(b"") is None
    assert _decode_body(b'{"a": 1}') == {"a": 1}
    assert _decode_body(b"data: hi\n\n") == "data: hi\n\n"  # SSE text kept readable
    assert _decode_body(b"\xff\xfe") == {"_b64": "//4="}  # binary → base64 wrapper
