from __future__ import annotations

from pathlib import Path

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


def test_doctor_passes_with_only_sbx_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An sbx-only host (docker sandbox uninstalled — the ideal post-migration state) must PASS:
    # doctor probes BOTH backends and requires only that at least one is present.
    _all_green(monkeypatch)
    monkeypatch.setattr(doctor, "_cmd_ok", lambda *cmd: cmd[:2] != ("docker", "sandbox"))
    assert doctor.run_doctor() == 0
    assert "found: sbx" in capsys.readouterr().out


def test_doctor_fails_when_no_sandbox_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # Neither sbx nor docker sandbox present -> the one required sandbox-CLI failure.
    _all_green(monkeypatch)
    absent = {("sbx", "version"), ("docker", "sandbox", "version")}
    monkeypatch.setattr(doctor, "_cmd_ok", lambda *cmd: cmd not in absent)
    assert doctor.run_doctor() == 1


def test_doctor_validates_present_danno_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A present, valid danno.toml passes the Configuration check (no added failures).
    _all_green(monkeypatch)
    example = (Path(__file__).resolve().parents[1] / "danno.toml.example").read_text()
    (tmp_path / "danno.toml").write_text(example)
    assert doctor.run_doctor(target=tmp_path) == 0


def test_doctor_invalid_danno_toml_is_required_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A present-but-broken config must FAIL here (required), loud — not pass doctor clean and
    # explode later at install/validate. Every runtime check is green, so this is the only fail.
    _all_green(monkeypatch)
    (tmp_path / "danno.toml").write_text("this is : not [valid toml")
    assert doctor.run_doctor(target=tmp_path) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "danno.toml" in out


def test_doctor_absent_danno_toml_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # No config yet is the normal pre-`install` state: a dim note, never a FAIL.
    _all_green(monkeypatch)
    assert doctor.run_doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "no danno.toml" in out and "FAIL" not in out
