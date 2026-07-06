"""Unit tests for the M7 `danno bench` orchestration (`suites.bench`) and the shared
AUT resolver (`suites.aut`). No Docker: dry-run returns without provisioning, and the
resolver/naming are pure."""

from __future__ import annotations

import json
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


def test_seed_opencode_config_writes_provider_and_models(tmp_path: Path) -> None:
    # opencode reads its provider/model registry from .opencode/opencode.jsonc; bench must
    # seed it (validate does so via prepare_workspace) or every turn fails "Model not found".
    bench._seed_opencode_config(_config(), "opencode", tmp_path)
    jsonc = tmp_path / ".opencode" / "opencode.jsonc"
    assert jsonc.is_file()
    body = jsonc.read_text(encoding="utf-8")
    assert "ollama" in body  # the provider is declared
    assert "qwen3:latest" in body  # the model registry is declared


def test_seed_opencode_config_noop_for_non_opencode_agents(tmp_path: Path) -> None:
    # claurst/occ/claude dial Ollama through the relay or a cloud provider, not opencode.jsonc.
    for agent in ("claurst", "occ", "claude"):
        bench._seed_opencode_config(_config(), agent, tmp_path)
        assert not (tmp_path / ".opencode" / "opencode.jsonc").exists()


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

    def fake_write(
        report, *, config_path, agent, variants, num_ctx_by_model=None, capture_dir=None
    ):  # type: ignore[no-untyped-def]
        captured["models"] = [v.model_ref for v in variants]
        captured["model_names"] = [v.model_name for v in variants]
        path = report.out_dir / "bench.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"agent": agent, "models": [v.model_ref for v in variants], "results": []}
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    # no suites enabled → no provisioning; we only assert the variant collapse + env-file.
    monkeypatch.setattr(bench, "_write_results", fake_write)
    opts = bench.BenchOptions(target=tmp_path, agent="claude", out_dir=tmp_path / "out")
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


def test_setup_bench_capture_off_is_a_noop() -> None:
    # No --capture → the original config, no binding, the default egress, no relay port.
    cfg = _config()
    opts = bench.BenchOptions(target=Path("."), agent="opencode", capture=False)
    cfg_for_run, binding, allow, port = bench._setup_bench_capture(cfg, opts, Path("/out"))
    assert cfg_for_run is cfg  # the original config, unrewritten
    assert binding is None
    assert allow == bench.sb.DEFAULT_ALLOW_HOSTS
    assert port is None


def test_setup_bench_capture_on_rewrites_backend_and_opens_port(tmp_path: Path) -> None:
    # --capture rewrites the ollama backend base_url at a proxy, opens its egress port,
    # and reports that port as the occ/claurst relay upstream (capture_port).
    cfg = _config()
    opts = bench.BenchOptions(target=Path("."), agent="opencode", capture=True)
    cfg_for_run, binding, allow, port = bench._setup_bench_capture(cfg, opts, tmp_path)
    assert binding is not None and port is not None
    # base_url now dials host.docker.internal:<proxy-port> (the recording proxy).
    assert "host.docker.internal" in cfg_for_run.backends["ollama"].base_url
    assert cfg.backends["ollama"].base_url == "http://h:11434/v1"  # original untouched
    assert f"localhost:{port}" in allow  # the egress hole for the proxy
    assert bench.sb.DEFAULT_ALLOW_HOSTS[0] in allow


def test_capture_binding_namespaces_per_permutation(tmp_path: Path) -> None:
    from book_em_danno.capture.wiring import CaptureBinding, CaptureTarget

    binding = CaptureBinding(
        targets=(
            CaptureTarget(
                backend_name="ollama",
                real_base_url="http://h:11434/v1",
                upstream="http://127.0.0.1:11434",
                proxy_port=9999,
                capture_file=tmp_path / "ollama.jsonl",
            ),
        ),
        capture_dir=tmp_path / "captures",
    )
    per = binding.permutation_targets(
        suite="aider", task_id="python/grade-school", model="ollama/qwen3:latest"
    )
    assert per[0].capture_file == (
        tmp_path / "captures" / "aider" / "python-grade-school" / "ollama-qwen3-latest.ollama.jsonl"
    )
    # a null model (claude reference row) still gets a stable segment
    dflt = binding.permutation_targets(suite="aider", task_id="t", model=None)
    assert dflt[0].capture_file.name == "default.ollama.jsonl"


def test_run_turn_for_occ_forwards_capture_port(monkeypatch: pytest.MonkeyPatch) -> None:
    # --capture threads the proxy port into occ's relay upstream (capture_port).
    from danno_validator import occ

    seen: dict[str, object] = {}

    def fake_occ_run(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        seen.update(kw)
        return object()

    monkeypatch.setattr(occ, "occ_run", fake_occ_run)
    aut.run_turn_for("occ", None, capture_port=7777)(Runner(), "box", "go", model="ollama/x")
    assert seen["capture_port"] == 7777


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
