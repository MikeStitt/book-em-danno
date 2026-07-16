"""Unit tests for the M7 `danno bench` orchestration (`suites.bench`) and the shared
HUT resolver (`suites.aut`). No Docker: dry-run returns without provisioning, and the
resolver/naming are pure."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from book_em_danno.config.schema import (
    DannoConfig,
    InertBackend,
    Model,
    OllamaBackend,
    OpenAIBackend,
)
from book_em_danno.core import exec as exec_mod
from book_em_danno.core.exec import CommandFailedError, Runner
from danno_validator import baseline
from danno_validator.matrix import model_variants
from danno_validator.suites import aut, bench
from danno_validator.suites.config import BenchmarksConfig


def _config() -> DannoConfig:
    return DannoConfig(
        backends={"ollama": OllamaBackend(kind="ollama", base_url="http://h:11434/v1")},
        models={
            "qwen": Model(
                backend="ollama", tag="qwen3:latest", context_budget=32000, output_limit=8192
            )
        },
        agents={"build": "qwen"},
    )


def _cloud_config() -> DannoConfig:
    """A mixed local + NVIDIA-NIM cloud matrix: `qwen` (Ollama) and `nemo` (openai)."""
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://h:11434/v1"),
            "nv": OpenAIBackend(
                kind="openai",
                base_url="https://integrate.api.nvidia.com/v1",
                api_key_env="NVIDIA_API_KEY",
            ),
        },
        models={
            "qwen": Model(
                backend="ollama", tag="qwen3:latest", context_budget=32000, output_limit=8192
            ),
            "nemo": Model(
                backend="nv",
                tag="nvidia/nemotron-super-49b",
                context_budget=128000,
                output_limit=8192,
            ),
        },
        agents={"build": "qwen"},
    )


def _cleanup_env_files(files: dict[str, Path | None]) -> None:
    for p in {p for p in files.values() if p is not None}:
        p.unlink(missing_ok=True)


def test_resolve_image_maps_claurst_to_shell() -> None:
    assert aut.resolve_image("claurst") == "shell"
    assert aut.resolve_image("opencode") == "opencode"


def test_resolve_image_claude_is_prebuilt_image() -> None:
    # claude is a prebuilt `docker sandbox create claude` image (the cloud reference HUT).
    assert aut.resolve_image("claude") == "claude"


def test_run_turn_for_claude_requires_env_file() -> None:
    # claude's turn producer needs an auth env-file — a None reaching it is a bug, not a
    # local run (unlike opencode/claurst/occ which accept None for the no-secrets local case).
    with pytest.raises(ValueError, match="auth env-file"):
        aut.run_turn_for("claude", None)
    assert callable(aut.run_turn_for("claude", Path("/tmp/danno-claude-auth")))


def test_build_bench_env_files_occ_carries_knob_defaults_overridable(tmp_path: Path) -> None:
    # occ's level-4 loop-ceiling knobs seed the file; danno.toml [env] composes on top.
    cfg = DannoConfig(
        backends={"ollama": OllamaBackend(kind="ollama", base_url="http://h:11434/v1")},
        models={
            "qwen": Model(
                backend="ollama", tag="qwen3:latest", context_budget=32000, output_limit=8192
            )
        },
        env={"CLAUDE_CODE_MAX_RECURSION_DEPTH": "5"},  # [env] lowers the generous default
    )
    opts = bench.BenchOptions(target=tmp_path, harness="occ")
    variants = model_variants(cfg)
    files = bench._build_bench_env_files(cfg, opts, variants)
    path = files[variants[0].model_ref]
    assert path is not None
    body = path.read_text(encoding="utf-8")
    _cleanup_env_files(files)
    assert "CLAUDE_CODE_API_TIMEOUT=" in body  # the level-4 default survives
    assert "CLAUDE_CODE_MAX_RECURSION_DEPTH=5" in body  # [env] beat the default
    assert "CLAUDE_CODE_MAX_RECURSION_DEPTH=500" not in body


def test_build_bench_env_files_occ_cloud_variant_injects_openai_base_and_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A cloud (openai) variant carries occ's OPENAI_BASE_URL + OPENAI_API_KEY mapping; the
    # local variant does NOT. Distinct env-files per row is what fixed the cloud 404 (occ
    # fell back to api.anthropic.com when no per-variant auth was injected).
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-secret")
    cfg = _cloud_config()
    opts = bench.BenchOptions(target=tmp_path, harness="occ")
    variants = model_variants(cfg)  # sorted: nemo (cloud), qwen (local)
    files = bench._build_bench_env_files(cfg, opts, variants)
    by_name = {v.model_name: files[v.model_ref] for v in variants}
    cloud_body = by_name["nemo"].read_text(encoding="utf-8")  # type: ignore[union-attr]
    local_body = by_name["qwen"].read_text(encoding="utf-8")  # type: ignore[union-attr]
    _cleanup_env_files(files)
    assert "OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1" in cloud_body
    assert "OPENAI_API_KEY=nv-secret" in cloud_body
    assert "OPENAI_API_KEY=" not in local_body  # local Ollama needs no cloud auth


def test_build_bench_env_files_opencode_cloud_variant_injects_raw_provider_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # opencode/claurst read the provider key under its OWN var (the generated provider block
    # references {env:NVIDIA_API_KEY}), not occ's OPENAI_* mapping.
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-secret")
    cfg = _cloud_config()
    for harness in ("opencode", "claurst"):
        opts = bench.BenchOptions(target=tmp_path, harness=harness)
        variants = model_variants(cfg)
        files = bench._build_bench_env_files(cfg, opts, variants)
        by_name = {v.model_name: files[v.model_ref] for v in variants}
        cloud_body = by_name["nemo"].read_text(encoding="utf-8")  # type: ignore[union-attr]
        _cleanup_env_files(files)
        assert "NVIDIA_API_KEY=nv-secret" in cloud_body, harness
        assert "OPENAI_API_KEY=" not in cloud_body, harness


def test_build_bench_env_files_cloud_variant_fails_loud_without_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A cloud row with its provider key unset fails loud HERE (before any sandbox is
    # provisioned), naming the missing var — not mid-session at an auth failure.
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    cfg = _cloud_config()
    opts = bench.BenchOptions(target=tmp_path, harness="occ")
    with pytest.raises(CommandFailedError, match="NVIDIA_API_KEY"):
        bench._build_bench_env_files(cfg, opts, model_variants(cfg))


def test_seed_opencode_config_writes_provider_and_models(tmp_path: Path) -> None:
    # opencode reads its provider/model registry from .opencode/opencode.jsonc; bench must
    # seed it (validate does so via prepare_workspace) or every turn fails "Model not found".
    bench._seed_opencode_config(_config(), "opencode", tmp_path)
    jsonc = tmp_path / ".opencode" / "opencode.jsonc"
    assert jsonc.is_file()
    body = jsonc.read_text(encoding="utf-8")
    assert "ollama" in body  # the provider is declared
    assert "qwen3:latest" in body  # the model registry is declared


def test_seed_opencode_config_noop_for_non_opencode_harnesses(tmp_path: Path) -> None:
    # claurst/occ/claude dial Ollama through the relay or a cloud provider, not opencode.jsonc.
    for harness in ("claurst", "occ", "claude"):
        bench._seed_opencode_config(_config(), harness, tmp_path)
        assert not (tmp_path / ".opencode" / "opencode.jsonc").exists()


def test_build_bench_env_files_claude_uses_host_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # claude does NOT flow through assemble_harness_env: every variant maps to the single auth
    # file, built from a host token (fail-loud without one).
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
    opts = bench.BenchOptions(target=tmp_path, harness="claude")
    variants = [baseline.baseline_variant(None)]
    files = bench._build_bench_env_files(_config(), opts, variants)
    path = files[variants[0].model_ref]
    assert path is not None
    body = path.read_text(encoding="utf-8")
    path.unlink(missing_ok=True)
    assert "CLAUDE_CODE_OAUTH_TOKEN=tok-abc" in body


def test_build_bench_env_files_claude_fails_loud_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    opts = bench.BenchOptions(target=tmp_path, harness="claude")
    with pytest.raises(CommandFailedError):
        bench._build_bench_env_files(_config(), opts, [baseline.baseline_variant(None)])


def test_run_bench_claude_collapses_matrix_to_reference_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # --harness claude ignores the local model matrix: a single `claude-code` row is written.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
    captured: dict[str, object] = {}

    def fake_write(
        report,
        *,
        config_path,
        harness,
        variants,
        num_ctx_by_model=None,
        capture_dir=None,
        captures_persisted=True,
    ):  # type: ignore[no-untyped-def]
        captured["models"] = [v.model_ref for v in variants]
        captured["model_names"] = [v.model_name for v in variants]
        path = report.out_dir / "bench.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"harness": harness, "models": [v.model_ref for v in variants], "results": []}
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return path

    # no suites enabled → no provisioning; we only assert the variant collapse + env-file.
    monkeypatch.setattr(bench, "_write_results", fake_write)
    opts = bench.BenchOptions(target=tmp_path, harness="claude", out_dir=tmp_path / "out")
    report = bench.run_bench(_config(), BenchmarksConfig(), opts, Runner())
    assert captured["model_names"] == ["claude-code"]  # one reference row, not per local model
    assert report.verdicts == []


def _claude_config() -> DannoConfig:
    """A config declaring inert-backend claude models to sweep, plus a local model."""
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://h:11434/v1"),
            "claude": InertBackend(kind="inert"),
        },
        models={
            "qwen": Model(
                backend="ollama", tag="qwen3:latest", context_budget=32000, output_limit=8192
            ),  # not claude's to run
            "opus": Model(backend="claude", tag="claude-opus-4-8"),
            "sonnet": Model(backend="claude", tag="claude-sonnet-4-6"),
        },
        agents={"build": "qwen"},
    )


def test_claude_inert_models_discovers_and_only_filters() -> None:
    cfg = _claude_config()
    # sorted danno keys of inert-backend models only — the local `qwen` is excluded
    assert bench._claude_inert_models(cfg, None) == ["opus", "sonnet"]
    assert bench._claude_inert_models(cfg, ["opus"]) == ["opus"]
    assert bench._claude_inert_models(_config(), None) == []  # no inert → collapse fallback


def test_harness_dial_ref_claude_inert_model_is_its_tag() -> None:
    cfg = _claude_config()
    (opus,) = [v for v in model_variants(cfg, only=["opus"])]
    (qwen,) = [v for v in model_variants(cfg, only=["qwen"])]
    # inert model → its tag is the claude --model value
    assert bench._harness_dial_ref("claude", cfg, opus) == "claude-opus-4-8"
    # a local model is not claude's to run → None (claude default); ditto the baseline row
    assert bench._harness_dial_ref("claude", cfg, qwen) is None
    assert bench._harness_dial_ref("claude", cfg, baseline.baseline_variant(None)) is None


def test_run_bench_claude_sweeps_declared_inert_models(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # With inert models declared, --harness claude sweeps them (one row each), NOT the
    # single collapsed reference row and NOT the local qwen model.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
    captured: dict[str, object] = {}

    def fake_write(
        report,
        *,
        config_path,
        harness,
        variants,
        num_ctx_by_model=None,
        capture_dir=None,
        captures_persisted=True,
    ):  # type: ignore[no-untyped-def]
        captured["model_names"] = [v.model_name for v in variants]
        captured["model_refs"] = [v.model_ref for v in variants]
        path = report.out_dir / "bench.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"harness": harness, "results": []}) + "\n", encoding="utf-8")
        return path

    monkeypatch.setattr(bench, "_write_results", fake_write)
    opts = bench.BenchOptions(target=tmp_path, harness="claude", out_dir=tmp_path / "out")
    bench.run_bench(_claude_config(), BenchmarksConfig(), opts, Runner())
    assert captured["model_names"] == ["opus", "sonnet"]  # swept, not collapsed
    assert captured["model_refs"] == ["claude-opus-4-8", "claude-sonnet-4-6"]  # bare tags


def test_run_turn_for_opencode_pins_build_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(exec_mod.subprocess, "run", fake_run)
    aut.run_turn_for("opencode", None)(Runner(), "box", "go", model="ollama/x")
    # opencode HUT drives its read-write run-agent so benchmark edits land.
    assert "--agent" in seen["cmd"] and "build" in seen["cmd"]


def test_run_turn_for_claude_threads_model_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # aut → baseline → driver: the per-variant model_override reaches claude's --model.
    seen: dict[str, object] = {}

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    monkeypatch.setattr(exec_mod.subprocess, "run", fake_run)
    envf = tmp_path / "auth"
    envf.write_text("CLAUDE_CODE_OAUTH_TOKEN=t\n", encoding="utf-8")
    aut.run_turn_for("claude", envf, model_override="claude-opus-4-8")(Runner(), "box", "go")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-8"


def test_run_turn_for_claude_uses_default_when_no_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    monkeypatch.setattr(exec_mod.subprocess, "run", fake_run)
    envf = tmp_path / "auth"
    envf.write_text("CLAUDE_CODE_OAUTH_TOKEN=t\n", encoding="utf-8")
    aut.run_turn_for("claude", envf)(Runner(), "box", "go")
    assert "--model" not in seen["cmd"]  # no override → claude's install default


def test_run_turn_for_claurst_returns_callable() -> None:
    assert callable(aut.run_turn_for("claurst", None))


def test_setup_bench_capture_no_save_is_always_on_but_never_persists(tmp_path: Path) -> None:
    # Capture is ALWAYS on (it powers the runaway gates); --no-save-captures now runs the SAME
    # proxy as a pure gate sensor with persist=False — no temp dir, no mkdtemp. The capture_dir
    # path is still computed (it names the numeric metrics sidecar), but the proxy never creates
    # it: nothing to strand or clean (this supersedes the F4 temp-root leak fix).
    cfg = _config()
    opts = bench.BenchOptions(target=Path("."), harness="opencode", save_captures=False)
    cfg_for_run, binding, allow, port = bench._setup_bench_capture(cfg, opts, tmp_path)
    assert binding is not None and port is not None  # proxy runs → feeds the gate tally
    assert binding.persist is False  # metrics-only: proxies write no JSONL/transcript
    assert binding.capture_dir == tmp_path / "captures"  # path computed, but never created
    assert not binding.capture_dir.exists()
    assert "host.docker.internal" in cfg_for_run.backends["ollama"].base_url


def test_run_bench_no_save_captures_creates_no_capture_dir_on_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Supersedes F4: under --no-save-captures the proxy writes nothing, so an abort AFTER
    # capture setup cannot strand wire captures — there is no capture dir (in /tmp or under
    # <out>) to leak, and none is ever created.
    opts = bench.BenchOptions(
        target=Path("."), harness="opencode", save_captures=False, out_dir=tmp_path
    )
    captured: dict[str, Path] = {}
    real_setup = bench._setup_bench_capture

    def _spy(config: DannoConfig, o: bench.BenchOptions, out: Path):  # type: ignore[no-untyped-def]
        result = real_setup(config, o, out)
        assert result[1] is not None
        captured["dir"] = result[1].capture_dir
        return result

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("abort mid-run")

    monkeypatch.setattr(bench, "_setup_bench_capture", _spy)
    monkeypatch.setattr(bench, "_setup_bench_sampler", _boom)  # raise right after capture setup

    with pytest.raises(RuntimeError, match="abort mid-run"):
        bench.run_bench(_config(), BenchmarksConfig(), opts, Runner())

    assert not captured["dir"].exists()  # never created — nothing to strand


def test_setup_bench_capture_default_saves_under_out_and_opens_port(tmp_path: Path) -> None:
    # Default (save) rewrites the ollama backend base_url at a proxy, opens its egress port,
    # persists under <out>/captures, and reports that port as the occ/claurst relay upstream.
    cfg = _config()
    opts = bench.BenchOptions(target=Path("."), harness="opencode")  # save_captures defaults True
    cfg_for_run, binding, allow, port = bench._setup_bench_capture(cfg, opts, tmp_path)
    assert binding is not None and port is not None
    assert binding.capture_dir == tmp_path / "captures"  # persisted under <out>
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


def test_run_turn_for_occ_forwards_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # run_turn_for threads the normalized dial ref into occ_run (item-3 fix).
    from danno_validator import occ

    seen: dict[str, object] = {}
    monkeypatch.setattr(occ, "occ_run", lambda r, n, p, **kw: seen.update(kw) or object())
    aut.run_turn_for("occ", None, model_override="ollama/qwen")(
        Runner(), "box", "go", model="danno-ollama/qwen"
    )
    assert seen["model"] == "ollama/qwen"


def test_run_turn_for_claurst_forwards_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from danno_validator import claurst

    seen: dict[str, object] = {}
    monkeypatch.setattr(claurst, "claurst_run", lambda r, n, p, **kw: seen.update(kw) or object())
    aut.run_turn_for("claurst", None, model_override="ollama/qwen")(
        Runner(), "box", "go", model="danno-ollama/qwen"
    )
    assert seen["model"] == "ollama/qwen"


def _dial_config() -> DannoConfig:
    """An Ollama backend named NOT `ollama` (the item-3 trigger) plus an NVIDIA cloud one."""
    return DannoConfig(
        backends={
            "danno-ollama": OllamaBackend(kind="ollama", base_url="http://h:11434/v1"),
            "nv": OpenAIBackend(
                kind="openai",
                base_url="https://integrate.api.nvidia.com/v1",
                api_key_env="NVIDIA_API_KEY",
            ),
        },
        models={
            "qwen": Model(
                backend="danno-ollama",
                tag="qwen3-coder-next",
                context_budget=32000,
                output_limit=8192,
            ),
            "nemo": Model(
                backend="nv",
                tag="nvidia/nemotron-super-49b",
                context_budget=128000,
                output_limit=8192,
            ),
        },
        agents={"build": "qwen"},
    )


def test_harness_dial_ref_occ_local_normalizes_backend_name() -> None:
    # The reported ref is `danno-ollama/…` (misread as cloud); the dial ref is `ollama/…`.
    cfg = _dial_config()
    (qwen,) = [v for v in model_variants(cfg) if v.model_name == "qwen"]
    assert qwen.model_ref == "danno-ollama/qwen3-coder-next"
    assert bench._harness_dial_ref("occ", cfg, qwen) == "ollama/qwen3-coder-next"


def test_harness_dial_ref_claurst_local_normalizes_backend_name() -> None:
    cfg = _dial_config()
    (qwen,) = [v for v in model_variants(cfg) if v.model_name == "qwen"]
    assert bench._harness_dial_ref("claurst", cfg, qwen) == "ollama/qwen3-coder-next"


def test_harness_dial_ref_cloud_matches_reported_ref_for_occ() -> None:
    # A cloud (openai) backend's dial ref equals its reported `<backend>/<tag>` ref.
    cfg = _dial_config()
    (nemo,) = [v for v in model_variants(cfg) if v.model_name == "nemo"]
    assert bench._harness_dial_ref("occ", cfg, nemo) == nemo.model_ref


def test_harness_dial_ref_opencode_and_claude_are_none() -> None:
    # opencode (provider = backend name in opencode.jsonc) and claude need no override.
    cfg = _dial_config()
    (qwen,) = [v for v in model_variants(cfg) if v.model_name == "qwen"]
    assert bench._harness_dial_ref("opencode", cfg, qwen) is None
    assert bench._harness_dial_ref("claude", cfg, qwen) is None


def test_sandbox_name_sanitises_instance_ids() -> None:
    name = bench._sandbox_name(Path("/tmp/proj"), "swe-astropy__astropy-12907")
    assert "__" not in name  # underscores -> hyphens for a valid sandbox name
    assert name.startswith("danno-")


def _gate_verdict(fc, rationale: str):  # type: ignore[no-untyped-def]
    from danno_validator.oracle import TurnVerdict

    return TurnVerdict(
        failure_class=fc,
        promised_action=False,
        tool_call_count=2,
        side_effect=False,
        rationale=rationale,
    )


def test_result_row_serialises_gate_observability_for_a_killed_cell(tmp_path: Path) -> None:
    from book_em_danno.core.exec import GateBreach
    from danno_validator.oracle import FailureClass
    from danno_validator.suites.base import BenchVerdict

    v = BenchVerdict(
        task_id="python/proverb",
        suite="aider",
        passed=False,
        verdict=_gate_verdict(FailureClass.RUNAWAY, "runaway: 11 rounds > 8"),
        tool_calls=2,
        tokens=100,
        cost=0.0,
        latency_s=12.34,
        model="ollama/stub",
        rounds=11,
        gate=GateBreach("runaway", 11, 8),
        survivors=(4321,),
        termination="gate_kill",
    )
    row = bench._result_row(v, num_ctx_by_model={}, out_dir=tmp_path, capture_dir=None)
    assert row["termination"] == "gate_kill"
    assert row["rounds"] == 11  # Gate-1 count...
    assert row["tool_calls"] == 2  # ...a distinct axis from tool_calls
    assert row["gate"] == {"gate": "runaway", "observed": 11, "limit": 8}
    assert row["survivors"] == [4321]


def test_result_row_omits_gate_fields_for_a_clean_ungated_cell(tmp_path: Path) -> None:
    from danno_validator.oracle import FailureClass
    from danno_validator.suites.base import BenchVerdict

    v = BenchVerdict(
        task_id="python/proverb",
        suite="aider",
        passed=True,
        verdict=_gate_verdict(FailureClass.PASS, "ok"),
        tool_calls=3,
        tokens=200,
        cost=0.0,
        latency_s=5.0,
        model="ollama/stub",
    )
    row = bench._result_row(v, num_ctx_by_model={}, out_dir=tmp_path, capture_dir=None)
    assert row["termination"] == "completed"  # always present
    assert "rounds" not in row  # ungated → no round count
    assert "gate" not in row  # no breach
    assert "survivors" not in row  # clean


def test_run_bench_dry_run_does_not_provision(tmp_path: pytest.TempPathFactory) -> None:
    opts = bench.BenchOptions(target=Path("."), harness="claurst", dry_run=True)
    cfg = BenchmarksConfig()
    cfg.aider_polyglot.enabled = True
    cfg.aider_polyglot.select = ["python/anagram"]
    report = bench.run_bench(_config(), cfg, opts, Runner())  # Runner() does not apply
    assert report.dry_run is True
    assert report.verdicts == []
    assert report.results_json is None


def test_resolve_bench_harnesses_defaults_to_single_opencode() -> None:
    # No CLI harnesses, no [harnesses] in benchmarks.toml → the single opencode default.
    assert bench.resolve_bench_harnesses(None, BenchmarksConfig()) == ["opencode"]


def test_resolve_bench_harnesses_reads_benchmarks_toml_list() -> None:
    cfg = BenchmarksConfig(harnesses=["occ", "claurst"])
    assert bench.resolve_bench_harnesses(None, cfg) == ["occ", "claurst"]


def test_resolve_bench_harnesses_cli_overrides_toml_and_dedupes() -> None:
    # --harness wins over benchmarks.toml [harnesses]; repeats collapse but order is preserved.
    cfg = BenchmarksConfig(harnesses=["occ"])
    assert bench.resolve_bench_harnesses(["claude", "occ", "claude"], cfg) == ["claude", "occ"]


def test_resolve_bench_harnesses_unknown_fails_loud() -> None:
    with pytest.raises(ValueError, match="unknown --harness 'gpt5'"):
        bench.resolve_bench_harnesses(["gpt5"], BenchmarksConfig())


def test_run_bench_harnesses_single_harness_runs_in_place(tmp_path: Path) -> None:
    # One harness → straight through run_bench into opts.out_dir, no comparison layer.
    opts = bench.BenchOptions(
        target=Path("."), harness="opencode", out_dir=tmp_path / "out", dry_run=True
    )
    reports = bench.run_bench_harnesses(_config(), BenchmarksConfig(), opts, Runner(), ["opencode"])
    assert len(reports) == 1
    assert reports[0].out_dir == tmp_path / "out"  # no per-harness subdir for the single case


def test_run_bench_harnesses_multi_harness_uses_per_harness_subdirs(tmp_path: Path) -> None:
    # Several harnesses → each into <root>/<harness>/; dry-run skips the comparison report.
    opts = bench.BenchOptions(
        target=Path("."), harness="opencode", out_dir=tmp_path / "root", dry_run=True
    )
    reports = bench.run_bench_harnesses(
        _config(), BenchmarksConfig(), opts, Runner(), ["occ", "claurst"]
    )
    root = tmp_path / "root"
    assert [r.out_dir for r in reports] == [root / "occ", root / "claurst"]


def test_warm_variant_warms_local_skips_cloud_and_respects_no_warm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_warm_variant` warms a local Ollama variant (appending its record), skips cloud refs
    (no local model), and is a no-op under `--no-warm`."""
    from danno_validator.matrix import ConfigVariant

    calls: list[str] = []

    def fake_warm(tag: str) -> dict:
        calls.append(tag)
        return {"tag": tag, "cache_hit": False, "warm_load_s": 0.1}

    monkeypatch.setattr(bench.ollama, "warm_model", fake_warm)
    local = ConfigVariant(model_name="a", model_ref="ollama/qwen:latest", description="")
    cloud = ConfigVariant(model_name="c", model_ref="anthropic/claude-x", description="")
    warmup: list[dict] = []

    bench._warm_variant(local, warm=True, warmup=warmup)
    bench._warm_variant(cloud, warm=True, warmup=warmup)  # cloud → skipped
    bench._warm_variant(local, warm=False, warmup=warmup)  # --no-warm → skipped
    assert calls == ["qwen:latest"]
    assert [w["tag"] for w in warmup] == ["qwen:latest"]


def test_warm_variant_records_each_call_so_reloads_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warming is just-in-time, NOT deduped: a model warmed again after an eviction (e.g. a
    task-major matrix alternating two models that don't co-fit) records a second cold-load —
    the thrash signal an up-front one-shot warm would hide."""
    from danno_validator.matrix import ConfigVariant

    monkeypatch.setattr(
        bench.ollama,
        "warm_model",
        lambda tag: {"tag": tag, "cache_hit": False, "warm_load_s": 2.0},
    )
    a = ConfigVariant(model_name="a", model_ref="ollama/a:latest", description="")
    b = ConfigVariant(model_name="b", model_ref="ollama/b:latest", description="")
    warmup: list[dict] = []
    # task 1: a, b ; task 2: a, b — a reloads on task 2 because b evicted it.
    for variant in (a, b, a, b):
        bench._warm_variant(variant, warm=True, warmup=warmup)
    assert [w["tag"] for w in warmup] == ["a:latest", "b:latest", "a:latest", "b:latest"]
