"""Unit tests for bench provenance (`telemetry.provenance`) and the Ollama /api/show
+ /api/tags provenance probes. Network/`/proc`/`nvidia-smi` are stubbed so the tests
stay hermetic and pass off the Linux/NVIDIA host."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from book_em_danno.commands import ollama
from book_em_danno.config.schema import DannoConfig, Model, OllamaBackend
from danno_validator.matrix import ConfigVariant
from danno_validator.telemetry import provenance


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


def test_parse_model_show_extracts_quant_params_and_ctx() -> None:
    body = {
        "details": {"quantization_level": "Q4_K_M", "parameter_size": "7.6B"},
        "model_info": {"general.architecture": "qwen3", "qwen3.context_length": 40960},
    }
    assert ollama._parse_model_show(body) == {
        "quantization": "Q4_K_M",
        "param_size": "7.6B",
        "architecture": "qwen3",
        "context_length": 40960,
    }


def test_parse_model_show_tolerates_missing_fields() -> None:
    assert ollama._parse_model_show({}) == {}
    # architecture present but no matching context_length key → no ctx emitted
    assert ollama._parse_model_show({"model_info": {"general.architecture": "llama"}}) == {
        "architecture": "llama"
    }


def test_parse_gpu_descriptor() -> None:
    rows = provenance._parse_gpu_descriptor("NVIDIA RTX 4090, 550.90, 24564\n")
    assert rows == [{"name": "NVIDIA RTX 4090", "driver": "550.90", "vram_total_mb": 24564.0}]


def test_model_provenance_skips_cloud_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    # A cloud ref has no local Ollama to probe → {} without any HTTP call.
    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise AssertionError("cloud ref must not probe Ollama")

    monkeypatch.setattr(ollama, "model_params", boom)
    monkeypatch.setattr(ollama, "model_digest", boom)
    assert provenance.model_provenance("anthropic/claude-sonnet-4-6") == {}


def test_model_provenance_probes_local_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ollama,
        "model_params",
        lambda tag, host: {"quantization": "Q4"} if tag == "qwen3:latest" else {},
    )
    monkeypatch.setattr(ollama, "model_digest", lambda tag, host: "sha256:abc")
    got = provenance.model_provenance("ollama/qwen3:latest")
    assert got == {"quantization": "Q4", "digest": "sha256:abc"}


class _Resp:
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> bool:
        return False

    def read(self) -> bytes:
        return b"{}"


def test_warm_model_cache_hit_skips_http(monkeypatch: pytest.MonkeyPatch) -> None:
    # A resident model needs no load: return cache_hit without touching /v1.
    monkeypatch.setattr(ollama, "running_models", lambda host: [{"name": "qwen3:latest"}])

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise AssertionError("resident model must not be re-warmed over HTTP")

    monkeypatch.setattr(ollama.urllib.request, "urlopen", boom)
    assert ollama.warm_model("qwen3:latest") == {
        "tag": "qwen3:latest",
        "cache_hit": True,
        "warm_load_s": 0.0,
    }


def test_warm_model_cold_load_measures_walltime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "running_models", lambda host: [])
    monkeypatch.setattr(ollama.urllib.request, "urlopen", lambda req, timeout=0: _Resp())
    ticks = iter([100.0, 100.5])
    monkeypatch.setattr(ollama.time, "monotonic", lambda: next(ticks))
    got = ollama.warm_model("qwen3:latest")
    assert got == {"tag": "qwen3:latest", "cache_hit": False, "warm_load_s": 0.5}


def test_warm_model_failure_returns_none_load(monkeypatch: pytest.MonkeyPatch) -> None:
    # A refused warm-up must not raise — the bench should still run (cell #1 pays the load).
    monkeypatch.setattr(ollama, "running_models", lambda host: [])

    def refuse(req, timeout=0):  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ollama.urllib.request, "urlopen", refuse)
    assert ollama.warm_model("qwen3:latest") == {
        "tag": "qwen3:latest",
        "cache_hit": False,
        "warm_load_s": None,
    }


def test_harness_provenance_records_danno_owned_pins() -> None:
    claurst_info = provenance.harness_provenance("claurst", _config())
    assert claurst_info["claurst_version"]  # the pinned release tag
    # opencode is image-provided: no danno-owned version pin
    assert provenance.harness_provenance("opencode", _config()) == {"harness": "opencode"}


def test_collect_and_write_provenance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(provenance, "host_descriptor", lambda: {"cpu_cores": 8})
    monkeypatch.setattr(
        provenance, "danno_version", lambda: {"version": "0.10.0", "commit": "abc1234"}
    )
    monkeypatch.setattr(
        provenance,
        "model_provenance",
        lambda ref, host=ollama.DEFAULT_HOST_URL: {"digest": "sha256:x"},
    )
    variants = [
        ConfigVariant(model_name="qwen", model_ref="ollama/qwen3:latest", description="qwen")
    ]
    payload = provenance.collect_provenance(
        _config(), variants, harness="opencode", sample_interval_s=0.5
    )
    assert payload["sample_interval_s"] == 0.5
    assert payload["host"] == {"cpu_cores": 8}
    assert payload["models"]["ollama/qwen3:latest"] == {"digest": "sha256:x"}
    assert payload["warmup"] == []  # default: no warm-up records unless passed
    path = provenance.write_provenance(tmp_path, payload)
    assert path == tmp_path / "provenance.json"
    assert json.loads(path.read_text())["danno"]["commit"] == "abc1234"


def test_collect_provenance_carries_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provenance, "host_descriptor", lambda: {})
    monkeypatch.setattr(provenance, "danno_version", lambda: {"version": None, "commit": None})
    monkeypatch.setattr(
        provenance, "model_provenance", lambda ref, host=ollama.DEFAULT_HOST_URL: {}
    )
    variants = [
        ConfigVariant(model_name="qwen", model_ref="ollama/qwen3:latest", description="qwen")
    ]
    warm = [{"tag": "qwen3:latest", "cache_hit": False, "warm_load_s": 1.2}]
    payload = provenance.collect_provenance(
        _config(), variants, harness="opencode", sample_interval_s=None, warmup=warm
    )
    assert payload["warmup"] == warm
