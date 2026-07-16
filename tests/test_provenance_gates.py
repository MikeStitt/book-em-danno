"""The runaway-gate config is recorded in bench provenance (reproducibility)."""

from __future__ import annotations

import pytest

from danno_validator.suites.config import GateLimits, GatesConfig
from danno_validator.telemetry import provenance as prov


def test_collect_provenance_records_resolved_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stub the host/harness probes so this is a pure serialization check (no subprocess).
    monkeypatch.setattr(prov, "host_descriptor", lambda: {})
    monkeypatch.setattr(prov, "harness_provenance", lambda harness, config: {})
    monkeypatch.setattr(prov, "danno_version", lambda: {})
    gates = GatesConfig(max_turns=40, harness={"opencode": GateLimits(max_turns=30)})
    payload = prov.collect_provenance(
        config=None,  # type: ignore[arg-type]  # unused once harness_provenance is stubbed
        variants=[],
        harness="opencode",
        sample_interval_s=None,
        gates=gates,
    )
    assert payload["gates"]["max_turns"] == 40
    assert payload["gates"]["harness"]["opencode"]["max_turns"] == 30


def test_collect_provenance_gates_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prov, "host_descriptor", lambda: {})
    monkeypatch.setattr(prov, "harness_provenance", lambda harness, config: {})
    monkeypatch.setattr(prov, "danno_version", lambda: {})
    payload = prov.collect_provenance(
        config=None,  # type: ignore[arg-type]
        variants=[],
        harness="opencode",
        sample_interval_s=None,
    )
    assert payload["gates"] is None
