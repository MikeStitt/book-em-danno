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


def test_resolve_image_claude_is_prebuilt_image() -> None:
    # claude is a prebuilt `docker sandbox create claude` image (the cloud reference AUT).
    assert aut.resolve_image("claude") == "claude"


def test_run_turn_for_claude_requires_env_file() -> None:
    # claude's turn producer needs an auth env-file — a None reaching it is a bug, not a
    # local run (unlike opencode/claurst/occ which accept None for the no-secrets local case).
    with pytest.raises(ValueError, match="auth env-file"):
        aut.run_turn_for("claude", None)
    assert callable(aut.run_turn_for("claude", Path("/tmp/danno-claude-auth")))


def test_build_bench_env_file_occ_carries_knob_defaults_overridable(tmp_path: Path) -> None:
    # occ's level-4 loop-ceiling knobs seed the file; danno.toml [env] composes on top.
    cfg = DannoConfig(
        backends={"ollama": OllamaBackend(kind="ollama", base_url="http://h:11434/v1")},
        models={"qwen": Model(backend="ollama", tag="qwen3:latest")},
        env={"CLAUDE_CODE_MAX_RECURSION_DEPTH": "5"},  # [env] lowers the generous default
    )
    path = bench._build_bench_env_file(cfg, "occ")
    assert path is not None
    body = path.read_text(encoding="utf-8")
    path.unlink(missing_ok=True)
    assert "CLAUDE_CODE_API_TIMEOUT=" in body  # the level-4 default survives
    assert "CLAUDE_CODE_MAX_RECURSION_DEPTH=5" in body  # [env] beat the default
    assert "CLAUDE_CODE_MAX_RECURSION_DEPTH=500" not in body


def test_build_bench_env_file_claude_uses_host_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # claude does NOT flow through assemble_agent_env: its file is the auth file, built
    # from a host token (fail-loud without one).
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
    path = bench._build_bench_env_file(_config(), "claude")
    assert path is not None
    body = path.read_text(encoding="utf-8")
    path.unlink(missing_ok=True)
    assert "CLAUDE_CODE_OAUTH_TOKEN=tok-abc" in body


def test_build_bench_env_file_claude_fails_loud_without_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from book_em_danno.core.exec import CommandFailedError

    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(CommandFailedError):
        bench._build_bench_env_file(_config(), "claude")


def test_run_bench_claude_collapses_matrix_to_reference_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # --agent claude ignores the local model matrix: a single `claude-code` row is written.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
    captured: dict[str, object] = {}

    def fake_write(report, *, config_path, agent, variants):  # type: ignore[no-untyped-def]
        captured["models"] = [v.model_ref for v in variants]
        captured["model_names"] = [v.model_name for v in variants]
        return tmp_path / "bench.json"

    # no suites enabled → no provisioning; we only assert the variant collapse + env-file.
    monkeypatch.setattr(bench, "_write_results", fake_write)
    opts = bench.BenchOptions(target=tmp_path, agent="claude")
    report = bench.run_bench(_config(), BenchmarksConfig(), opts, Runner())
    assert captured["model_names"] == ["claude-code"]  # one reference row, not per local model
    assert report.verdicts == []


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
