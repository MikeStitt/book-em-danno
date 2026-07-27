"""Unit tests for the Ollama Responses-API readiness probe (Phase 3, codex pre-flight).

`responses_api_ready` gates `danno bench --harness codex` and the doctor WARN: codex speaks
ONLY the OpenAI Responses API, which Ollama exposes at `/v1/responses` from 0.13.3. The probe
prefers a direct endpoint probe (empty POST → 400 = exists, not 404) and falls back to the
`/api/version` gate, returning None only when Ollama is unreachable. All network calls are
stubbed, so these run without a live Ollama."""

from __future__ import annotations

import io
import urllib.error

import pytest

from book_em_danno.commands import ollama


def test_version_tuple_parses_and_drops_suffix() -> None:
    assert ollama._version_tuple("0.13.3") == (0, 13, 3)
    assert ollama._version_tuple("0.30.6-rc1") == (0, 30, 6)  # non-numeric tail dropped
    assert ollama._version_tuple("1.0") == (1, 0)


def test_version_tuple_ordering_around_min() -> None:
    assert ollama._version_tuple("0.13.3") >= ollama.MIN_OLLAMA_FOR_RESPONSES
    assert ollama._version_tuple("0.14.0") >= ollama.MIN_OLLAMA_FOR_RESPONSES
    assert ollama._version_tuple("0.13.2") < ollama.MIN_OLLAMA_FOR_RESPONSES


def _fake_resp(status: int) -> io.BytesIO:
    buf = io.BytesIO(b"{}")
    buf.status = status  # type: ignore[attr-defined]
    return buf


def test_responses_ready_none_when_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "reachable", lambda *a, **k: False)
    assert ollama.responses_api_ready() is None  # distinguishes "no Ollama" from "too old"


def test_responses_ready_true_on_endpoint_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # An empty POST to an existing /v1/responses is rejected with 400 (bad body), NOT 404.
    monkeypatch.setattr(ollama, "reachable", lambda *a, **k: True)

    def fake_urlopen(req, timeout=0):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(ollama.urllib.request, "urlopen", fake_urlopen)
    assert ollama.responses_api_ready() is True


def test_responses_ready_false_on_endpoint_404(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 404 is authoritative: the endpoint is absent (old Ollama) → not ready.
    monkeypatch.setattr(ollama, "reachable", lambda *a, **k: True)

    def fake_urlopen(req, timeout=0):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(ollama.urllib.request, "urlopen", fake_urlopen)
    assert ollama.responses_api_ready() is False


def test_responses_ready_falls_back_to_version_gate_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When the POST probe can't complete (transport error), fall back to the /api/version gate.
    monkeypatch.setattr(ollama, "reachable", lambda *a, **k: True)

    def fake_urlopen(req, timeout=0):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(ollama.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ollama, "ollama_version", lambda *a, **k: "0.14.0")
    assert ollama.responses_api_ready() is True
    monkeypatch.setattr(ollama, "ollama_version", lambda *a, **k: "0.13.2")
    assert ollama.responses_api_ready() is False


def test_responses_ready_true_on_endpoint_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "reachable", lambda *a, **k: True)
    monkeypatch.setattr(ollama.urllib.request, "urlopen", lambda req, timeout=0: _fake_resp(200))
    assert ollama.responses_api_ready() is True
