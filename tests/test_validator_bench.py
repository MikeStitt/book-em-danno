"""Unit tests for the M7 `danno bench` orchestration (`suites.bench`) and the shared
AUT resolver (`suites.aut`). No Docker: dry-run returns without provisioning, and the
resolver/naming are pure."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from book_em_danno.config.schema import DannoConfig, Model, OllamaBackend
from book_em_danno.core import exec as exec_mod
from book_em_danno.core.exec import Runner
from danno_validator.suites import aut, bench
from danno_validator.suites.config import BenchmarksConfig


def _config() -> DannoConfig:
    return DannoConfig(
        backends={"ollama": OllamaBackend(kind="ollama", base_url="http://h:11434/v1")},
        models={"qwen": Model(backend="ollama", tag="qwen3:latest")},
        agents={"build": "qwen"},
    )


def test_resolve_image_maps_claurst_to_shell() -> None:
    assert aut.resolve_image("claurst") == "shell"
    assert aut.resolve_image("opencode") == "opencode"


def test_run_turn_for_opencode_pins_build_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(exec_mod.subprocess, "run", fake_run)
    aut.run_turn_for("opencode", None)(Runner(), "box", "go", model="ollama/x")
    # opencode AUT drives its read-write run-agent so benchmark edits land.
    assert "--agent" in seen["cmd"] and "build" in seen["cmd"]


def test_run_turn_for_claurst_returns_callable() -> None:
    assert callable(aut.run_turn_for("claurst", None))


def test_sandbox_name_sanitises_instance_ids() -> None:
    name = bench._sandbox_name(Path("/tmp/proj"), "swe-astropy__astropy-12907")
    assert "__" not in name  # underscores -> hyphens for a valid sandbox name
    assert name.startswith("danno-")


def test_run_bench_dry_run_does_not_provision(tmp_path: pytest.TempPathFactory) -> None:
    opts = bench.BenchOptions(target=Path("."), agent="claurst", dry_run=True)
    cfg = BenchmarksConfig()
    cfg.aider_polyglot.enabled = True
    cfg.aider_polyglot.select = ["python/anagram"]
    report = bench.run_bench(_config(), cfg, opts, Runner())  # Runner() does not apply
    assert report.dry_run is True
    assert report.verdicts == []
    assert report.results_json is None
