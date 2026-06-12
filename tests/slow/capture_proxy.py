"""A tiny recording HTTP proxy in front of host Ollama, for wire-capture tests.

The sandboxed opencode dials `host.docker.internal:11435/v1`; this proxy listens
there, appends each POST's `{path, body}` as a JSON line to a capture file, then
forwards to the real Ollama at `127.0.0.1:11434` and replays the response. We
buffer the upstream response fully before replaying — assertions are on the
*requests*, and opencode parses the SSE payload fine from a buffered body.

Committed test infrastructure (full standards), not a `scratch/` probe.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

UPSTREAM = "http://127.0.0.1:11434"


def _read_captures(path: Path) -> list[dict[str, Any]]:
    """Parse the capture file into a list of `{path, body}` records."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class _Handler(BaseHTTPRequestHandler):
    capture_file: Path  # set on the server, read via self.server

    def log_message(self, *_args: Any) -> None:  # noqa: D401 - silence default logging
        pass

    def _proxy(self, body: bytes | None) -> None:
        capture_file: Path = self.server.capture_file  # type: ignore[attr-defined]
        if body and self.command == "POST":
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = None
            if parsed is not None:
                with capture_file.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"path": self.path, "body": parsed}) + "\n")
        req = urllib.request.Request(
            f"{UPSTREAM}{self.path}",
            data=body,
            method=self.command,
            headers={"Content-Type": self.headers.get("Content-Type", "application/json")},
        )
        # Generous timeout: a cold model load + full generation can take minutes.
        with urllib.request.urlopen(req, timeout=600) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "application/json")
            payload = resp.read()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
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
def capture_proxy(capture_file: Path, *, port: int = 11435) -> Iterator[Path]:
    """Run the recording proxy on `0.0.0.0:<port>` for the duration of the block.

    Yields the capture-file path; read records from it with `read_captures`."""
    capture_file.write_text("", encoding="utf-8")
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    server.capture_file = capture_file  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield capture_file
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# Re-exported for tests.
read_captures = _read_captures
