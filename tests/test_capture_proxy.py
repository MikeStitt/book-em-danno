"""Unit tests for the recording-and-re-originating capture proxy.

Drives the proxy against a stub upstream HTTP server (no Docker): asserts it records
the request AND response, passes auth headers upstream while redacting them in the
capture, decodes bodies sensibly, and fails loud on a busy port.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from book_em_danno.capture.proxy import (
    CaptureProxyConfig,
    _CaptureServer,
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


def test_upstream_unreachable_logs_transient_and_returns_502(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Point the proxy at a dead port: the client gets a synthetic 502 AND the blip is
    # logged at TRANSIENT (retry-safe from the harness's side, policy §5) instead of
    # vanishing behind the 502 body.
    cfg = CaptureProxyConfig(
        upstream="http://127.0.0.1:1", capture_file=tmp_path / "cap.jsonl", port=_free_port()
    )
    with capture_proxy(cfg):
        req = urllib.request.Request(
            f"http://127.0.0.1:{cfg.port}/v1/chat/completions",
            data=json.dumps({"model": "m"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
            status = 200
        except urllib.error.HTTPError as exc:
            status = exc.code
    assert status == 502
    assert "[TRANSIENT]" in capsys.readouterr().err


def test_handler_error_is_recorded_and_counted(tmp_path: Path) -> None:
    # A crash escaping a handler thread must land as a synthetic `error` record + a bumped
    # `handler_errors` counter (policy §5), not a bare stderr traceback with a dangling
    # request record. Exercise the server hook directly from an active `except`.
    cfg = CaptureProxyConfig(
        upstream="http://127.0.0.1:1", capture_file=tmp_path / "cap.jsonl", port=_free_port()
    )
    server = _CaptureServer(cfg)
    try:
        try:
            raise RuntimeError("boom inside a handler")
        except RuntimeError:
            server.handle_error(request=None, client_address=("127.0.0.1", 5555))
        assert server.handler_errors == 1
        err = next(r for r in read_captures(cfg.capture_file) if r["direction"] == "error")
        assert "boom inside a handler" in err["error"]
        assert "RuntimeError" in err["traceback"]
    finally:
        server.server_close()


def test_handler_error_counts_but_writes_nothing_when_not_persisting(tmp_path: Path) -> None:
    cfg = CaptureProxyConfig(
        upstream="http://127.0.0.1:1",
        capture_file=tmp_path / "cap.jsonl",
        port=_free_port(),
        persist=False,
    )
    server = _CaptureServer(cfg)
    try:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            server.handle_error(request=None, client_address=("127.0.0.1", 5555))
        assert server.handler_errors == 1
        assert not cfg.capture_file.exists()  # --no-save-captures touches no disk
    finally:
        server.server_close()


def test_decode_body_variants() -> None:
    assert _decode_body(b"") is None
    assert _decode_body(b'{"a": 1}') == {"a": 1}
    assert _decode_body(b"data: hi\n\n") == "data: hi\n\n"  # SSE text kept readable
    assert _decode_body(b"\xff\xfe") == {"_b64": "//4="}  # binary → base64 wrapper
