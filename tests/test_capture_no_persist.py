"""Unit tests for the capture proxy in metrics-only mode (`persist=False`).

The `--no-save-captures` contract sharpened: the recording proxy still feeds the runaway-gate
tally and accumulates body-free `CallSummary` numbers in RAM, but writes NOTHING to disk — no
directory, no JSONL, no bodies. Drives the real proxy against a stub upstream (no Docker).
See `.docs/plan-no-capture-truely-does-not-capture.md` §7.
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

from book_em_danno.capture import proxy as proxy_mod
from book_em_danno.capture.gate import GateTally
from book_em_danno.capture.proxy import CaptureProxyConfig, capture_proxy

# Unique markers so the no-body assertion is unambiguous: if either substring appears in the
# retained summaries, message text leaked into the in-RAM sensor.
_PROMPT_MARKER = "PROMPT_MARKER_e29b41d4"
_COMPLETION_MARKER = "COMPLETION_MARKER_9f8c2a17"


class _Upstream(BaseHTTPRequestHandler):
    """POST → a usage-bearing completion echoing a marker; GET → a discovery body (no usage)."""

    def log_message(self, *_a: object) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        if length:
            self.rfile.read(length)
        payload = json.dumps(
            {
                "choices": [{"message": {"content": f"{_COMPLETION_MARKER} ok"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            }
        ).encode()
        self._send(payload)

    def do_GET(self) -> None:  # noqa: N802
        self._send(json.dumps({"models": []}).encode())

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


def _inference_post(port: int) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps({"messages": [{"role": "user", "content": _PROMPT_MARKER}]}).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=10).read()


def test_no_persist_feeds_tally_and_summaries_without_touching_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tally = GateTally()
    # The proxy's ONLY disk writer is `_Handler._record`; under persist=False it must never
    # fire. Trip the test loudly if it does (a stronger guard than a post-hoc dir check).
    monkeypatch.setattr(
        proxy_mod._Handler,
        "_record",
        lambda self, record: pytest.fail("_record wrote to disk under persist=False"),
    )
    cap_file = tmp_path / "sub" / "cap.jsonl"  # neither this nor its parent may be created
    with _upstream() as up_port:
        cfg = CaptureProxyConfig(
            upstream=f"http://127.0.0.1:{up_port}",
            capture_file=cap_file,
            port=_free_port(),
            tally=tally,
            persist=False,
        )
        with capture_proxy(cfg) as server:
            _inference_post(cfg.port)
            _inference_post(cfg.port)
            urllib.request.urlopen(f"http://127.0.0.1:{cfg.port}/api/tags", timeout=10).read()
        summaries = server.read_summaries()

    # (a) the gate tally still ticks per F1 (two inference rounds, tokens summed).
    assert tally.inference_calls() == 2
    assert tally.tokens() == 32
    assert not tally.blind()

    # (b) read_summaries() returns body-free numbers for every call (incl. the discovery GET).
    assert [(s.method, s.path) for s in summaries] == [
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/chat/completions"),
        ("GET", "/api/tags"),
    ]
    inference = [s for s in summaries if s.path == "/v1/chat/completions"]
    assert all(
        s.usage == {"prompt": 12, "completion": 4, "total": 16, "cached": None} for s in inference
    )

    # (c) NO file created anywhere — not the capture file, not its parent dir.
    assert not cap_file.exists()
    assert not cap_file.parent.exists()


def test_no_persist_retains_no_message_bodies(tmp_path: Path) -> None:
    # The "prompts never retained" check: no prompt/completion substring survives in the
    # in-RAM summary list — CallSummary carries counts + routing only, never message text.
    with _upstream() as up_port:
        cfg = CaptureProxyConfig(
            upstream=f"http://127.0.0.1:{up_port}",
            capture_file=tmp_path / "cap.jsonl",
            port=_free_port(),
            persist=False,
        )
        with capture_proxy(cfg) as server:
            _inference_post(cfg.port)
        dumped = json.dumps([s.__dict__ for s in server.read_summaries()])

    assert _PROMPT_MARKER not in dumped
    assert _COMPLETION_MARKER not in dumped


def test_persist_true_still_writes_and_summarizes(tmp_path: Path) -> None:
    # Save-mode is unchanged: the JSONL is written AND the same body-free summaries populate,
    # so both derivation paths remain available.
    from book_em_danno.capture.proxy import read_captures

    cap_file = tmp_path / "cap.jsonl"
    with _upstream() as up_port:
        cfg = CaptureProxyConfig(
            upstream=f"http://127.0.0.1:{up_port}",
            capture_file=cap_file,
            port=_free_port(),
            persist=True,
        )
        with capture_proxy(cfg) as server:
            _inference_post(cfg.port)
        summaries = server.read_summaries()

    assert cap_file.is_file()
    assert len(read_captures(cap_file)) == 2  # request + response
    assert len(summaries) == 1 and summaries[0].usage is not None
