"""V1 — the gate-sensor dialect matrix (the F1 net).

Drive the **real capture proxy** (feeding a live `GateTally`) against the stub AI, one
inference request per wire dialect the harnesses actually produce, and assert whether
Gate 1 (round count) ticked. Every inference dialect must tick — including the usage-less
ones (Ollama-native `/api/chat`, an OpenAI SSE stream without `stream_options.include_usage`)
that were the F1 blind spot: the sensor now counts rounds by request path, not by whether
`extract_usage` returned a value. Discovery `GET`s must not tick. The `rounds >=
request_count` invariant holds across every row. See `.docs/plan-runaway-gates-validation.md`
§2.1/§4.
"""

from __future__ import annotations

import json
import socket
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from book_em_danno.capture.gate import GateTally
from book_em_danno.capture.proxy import CaptureProxyConfig, capture_proxy, read_captures
from book_em_danno.stubai import Finish, StubConfig, stub_ai
from danno_validator.telemetry.wire_metrics import parse_capture_records

pytestmark = pytest.mark.timeout(30)


@dataclass(frozen=True)
class Row:
    """One wire dialect the sensor must recognise as an inference round."""

    method: str
    path: str
    payload: dict
    expected_ticks: int  # Gate-1 ticks this single request must produce


# label → Row. Every inference dialect (rows 1-6) must tick Gate 1; the usage-less rows
# (chat-sse-no-usage, both Ollama-native) are the F1 fix in action. Discovery does not tick.
_ROWS: dict[str, Row] = {
    "chat-nonstream-usage": Row(
        "POST", "/v1/chat/completions", {"model": "stub", "stream": False}, 1
    ),
    "chat-sse-include-usage": Row(
        "POST",
        "/v1/chat/completions",
        {"model": "stub", "stream": True, "stream_options": {"include_usage": True}},
        1,
    ),
    "chat-sse-no-usage": Row("POST", "/v1/chat/completions", {"model": "stub", "stream": True}, 1),
    "ollama-native-nonstream": Row("POST", "/api/chat", {"model": "stub", "stream": False}, 1),
    "ollama-native-ndjson": Row("POST", "/api/chat", {"model": "stub", "stream": True}, 1),
    "responses-sse": Row("POST", "/v1/responses", {"model": "stub"}, 1),
    "discovery-get": Row("GET", "/api/tags", {}, 0),
}


def _params() -> list:
    return [pytest.param(row, id=label) for label, row in _ROWS.items()]


@dataclass(frozen=True)
class _Wired:
    tally: GateTally
    capture_file: Path


@contextmanager
def _proxy_to_stub(tmp_path: Path) -> Iterator[tuple[_Wired, str]]:
    """client → capture proxy (with a fresh `GateTally`) → stub AI. Yields the wiring and
    the proxy base URL to send the row's single request at."""
    script = [Finish("ok")]
    with stub_ai(StubConfig(script=script, transcript_file=tmp_path / "stub.jsonl")) as stub:
        tally = GateTally()
        capture_file = tmp_path / "capture.jsonl"
        cfg = CaptureProxyConfig(
            upstream=stub.base_url,
            capture_file=capture_file,
            port=_free_port(),
            tally=tally,
        )
        with capture_proxy(cfg):
            yield _Wired(tally=tally, capture_file=capture_file), f"http://127.0.0.1:{cfg.port}"


def _send(base_url: str, row: Row) -> None:
    if row.method == "GET":
        urllib.request.urlopen(f"{base_url}{row.path}", timeout=15).read()
        return
    req = urllib.request.Request(
        f"{base_url}{row.path}",
        data=json.dumps(row.payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15).read()


@pytest.mark.parametrize("row", _params())
def test_gate1_ticks_per_dialect(row: Row, tmp_path: Path) -> None:
    with _proxy_to_stub(tmp_path) as (wired, base_url):
        _send(base_url, row)
    assert wired.tally.inference_calls() == row.expected_ticks


@pytest.mark.parametrize("row", _params())
def test_rounds_never_below_request_count(row: Row, tmp_path: Path) -> None:
    # Invariant across every dialect: the live sensor may never see FEWER rounds than the
    # post-hoc wire parser derives. Both now count by request path, so for an inference row
    # they agree exactly (rounds == request_count); it is the correctness guardrail.
    with _proxy_to_stub(tmp_path) as (wired, base_url):
        _send(base_url, row)
        rounds = wired.tally.inference_calls()
        request_count = len(parse_capture_records(read_captures(wired.capture_file)))
    assert rounds >= request_count


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]
