from __future__ import annotations

import pytest

from book_em_danno.commands import doctor, ollama


def _all_green(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_on_path", lambda name: True)
    monkeypatch.setattr(doctor, "_cmd_ok", lambda *cmd: True)
    monkeypatch.setattr(doctor, "_ollama_has_model", lambda: True)
    monkeypatch.setattr(ollama, "reachable", lambda *a, **k: True)
    monkeypatch.setattr(ollama, "lan_exposure_warning", lambda **k: None)
    monkeypatch.setattr(ollama, "responses_api_ready", lambda *a, **k: True)


def test_doctor_all_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_green(monkeypatch)
    assert doctor.run_doctor() == 0


def test_doctor_counts_required_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_green(monkeypatch)
    # Docker daemon down: _cmd_ok("docker","info") fails -> 1 required failure.
    monkeypatch.setattr(doctor, "_cmd_ok", lambda *cmd: cmd[:2] != ("docker", "info"))
    assert doctor.run_doctor() == 1


def test_lan_exposure_is_a_warning_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _all_green(monkeypatch)
    monkeypatch.setattr(ollama, "lan_exposure_warning", lambda **k: "rebind loopback-only")
    assert doctor.run_doctor() == 0  # WARN does not fail the preflight


def test_old_ollama_responses_is_a_warning_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # An Ollama too old for /v1/responses only affects `--harness codex`, so it WARNs.
    _all_green(monkeypatch)
    monkeypatch.setattr(ollama, "responses_api_ready", lambda *a, **k: False)
    assert doctor.run_doctor() == 0


def test_unreachable_ollama_skips_responses_check(monkeypatch: pytest.MonkeyPatch) -> None:
    # None (Ollama unreachable) → the codex Responses check is skipped, not a WARN/fail.
    _all_green(monkeypatch)
    monkeypatch.setattr(ollama, "responses_api_ready", lambda *a, **k: None)
    assert doctor.run_doctor() == 0
