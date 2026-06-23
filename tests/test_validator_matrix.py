"""Unit tests for the M2 matrix generator — pure config expansion, no sandbox."""

from __future__ import annotations

import pytest

from book_em_danno.config.schema import (
    DannoConfig,
    LlamacppBackend,
    Model,
    OllamaBackend,
    OpenAIBackend,
)
from danno_validator.matrix import model_variants


def _config() -> DannoConfig:
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1"),
            "nvidia": OpenAIBackend(
                kind="openai", base_url="https://nim/v1", api_key_env="NVIDIA_API_KEY"
            ),
        },
        models={
            "gemma": Model(backend="ollama", tag="gemma3:27b"),
            "nemotron": Model(backend="nvidia", tag="nvidia/nemotron"),
        },
        # cloud agents are raw inline refs and are NOT swept (not [models] entries).
        agents={"pm": "anthropic/claude-sonnet-4-6"},
    )


def test_one_variant_per_declared_model_sorted_by_key() -> None:
    variants = model_variants(_config())
    assert [v.model_name for v in variants] == ["gemma", "nemotron"]


def test_refs_resolve_per_backend_kind() -> None:
    refs = {v.model_name: v.model_ref for v in model_variants(_config())}
    assert refs["gemma"] == "ollama/gemma3:27b"  # ollama -> backend/tag
    assert refs["nemotron"] == "nvidia/nvidia/nemotron"  # openai -> backend/tag


def test_only_restricts_and_preserves_sort_order() -> None:
    variants = model_variants(_config(), only=["nemotron", "gemma"])
    assert [v.model_name for v in variants] == ["gemma", "nemotron"]


def test_only_unknown_model_fails_loud() -> None:
    with pytest.raises(ValueError, match="not declared"):
        model_variants(_config(), only=["gemma", "ghost"])


def test_unimplemented_backend_surfaces_at_expansion() -> None:
    cfg = DannoConfig(
        backends={"lc": LlamacppBackend(kind="llamacpp", base_url="http://localhost:8080/v1")},
        models={"local": Model(backend="lc", tag="whatever")},
    )
    with pytest.raises(NotImplementedError):
        model_variants(cfg)
