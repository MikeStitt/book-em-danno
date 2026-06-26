"""Unit tests for the capture config transform + allow-list helpers (no Docker)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from book_em_danno.capture.wiring import (
    capture_allow_hosts,
    plan_capture,
    uncaptured_cloud_refs,
)
from book_em_danno.commands.sandbox import _claurst_relay_capture_port
from book_em_danno.config.generate import render_config
from book_em_danno.config.schema import (
    DannoConfig,
    Defaults,
    LlamacppBackend,
    Model,
    OllamaBackend,
    OpenAIBackend,
)
from book_em_danno.core.exec import CommandFailedError


def _strip_comments(text: str) -> str:
    return "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("//"))


def _cfg() -> DannoConfig:
    return DannoConfig(
        defaults=Defaults(default_agent="build"),
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1"),
            "nv": OpenAIBackend(
                kind="openai",
                base_url="https://integrate.api.nvidia.com/v1",
                api_key_env="NVIDIA_API_KEY",
            ),
            # declared but unused → must be skipped by capture (and not raise the stub)
            "lcpp": LlamacppBackend(
                kind="llamacpp", base_url="http://host.docker.internal:8080/v1"
            ),
        },
        models={
            "g": Model(backend="ollama", tag="gemma3:27b"),
            "n": Model(backend="nv", tag="nvidia/x"),
        },
        agents={"build": "g", "pm": "anthropic/claude-sonnet-4-6"},
    )


def test_plan_capture_rewrites_ollama_and_openai_only(tmp_path: Path) -> None:
    cfg = _cfg()
    cfg2, targets = plan_capture(cfg, tmp_path / "caps")

    by = {t.backend_name: t for t in targets}
    assert set(by) == {"ollama", "nv"}  # llamacpp (stub) is left untouched
    assert len({t.proxy_port for t in targets}) == 2  # distinct ports

    # Ollama's host.docker.internal upstream rewrites to host loopback; cloud is verbatim.
    assert by["ollama"].upstream == "http://127.0.0.1:11434"
    assert by["nv"].upstream == "https://integrate.api.nvidia.com"

    # The rewritten config dials the proxies (always http), preserving the /v1 path.
    assert (
        cfg2.backends["ollama"].base_url
        == f"http://host.docker.internal:{by['ollama'].proxy_port}/v1"
    )
    assert cfg2.backends["nv"].base_url == f"http://host.docker.internal:{by['nv'].proxy_port}/v1"
    assert cfg2.backends["lcpp"].base_url == "http://host.docker.internal:8080/v1"

    # The original config is never mutated.
    assert cfg.backends["ollama"].base_url == "http://host.docker.internal:11434/v1"
    # Capture files are named per backend under the capture dir.
    assert by["ollama"].capture_file == tmp_path / "caps" / "ollama.jsonl"


def test_rewritten_config_renders_proxy_baseurls(tmp_path: Path) -> None:
    cfg2, targets = plan_capture(_cfg(), tmp_path / "caps")
    doc = json.loads(_strip_comments(render_config(cfg2)))
    rendered = {p["options"]["baseURL"] for p in doc["provider"].values()}
    for target in targets:
        assert f"http://host.docker.internal:{target.proxy_port}/v1" in rendered


def test_capture_allow_hosts_appends_a_hole_per_proxy(tmp_path: Path) -> None:
    _, targets = plan_capture(_cfg(), tmp_path / "caps")
    hosts = capture_allow_hosts(targets, ("localhost:11434",))
    assert hosts[0] == "localhost:11434"
    assert set(hosts[1:]) == {f"localhost:{t.proxy_port}" for t in targets}


def test_uncaptured_cloud_refs_flags_raw_refs() -> None:
    assert uncaptured_cloud_refs(_cfg()) == ["anthropic/claude-sonnet-4-6"]


def test_claurst_relay_capture_port_picks_host_ollama(tmp_path: Path) -> None:
    # claurst's relay always dials host Ollama; capture inserts the proxy fronting it.
    _, targets = plan_capture(_cfg(), tmp_path / "caps")
    ollama = next(t for t in targets if t.backend_name == "ollama")
    assert _claurst_relay_capture_port(targets) == ollama.proxy_port


def test_claurst_relay_capture_port_none_fails_loud(tmp_path: Path) -> None:
    cfg = DannoConfig(
        defaults=Defaults(default_agent="build"),
        backends={
            "nv": OpenAIBackend(
                kind="openai", base_url="https://integrate.api.nvidia.com/v1", api_key_env="K"
            )
        },
        models={"n": Model(backend="nv", tag="nvidia/x")},
        agents={"build": "n"},
    )
    _, targets = plan_capture(cfg, tmp_path / "caps")
    with pytest.raises(CommandFailedError, match="no Ollama backend"):
        _claurst_relay_capture_port(targets)


def test_claurst_relay_capture_port_ambiguous_fails_loud(tmp_path: Path) -> None:
    cfg = DannoConfig(
        defaults=Defaults(default_agent="build"),
        backends={
            "o1": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1"),
            "o2": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434"),
        },
        models={"a": Model(backend="o1", tag="x"), "b": Model(backend="o2", tag="y")},
        agents={"build": "a"},
    )
    _, targets = plan_capture(cfg, tmp_path / "caps")
    with pytest.raises(CommandFailedError, match="front host Ollama"):
        _claurst_relay_capture_port(targets)
