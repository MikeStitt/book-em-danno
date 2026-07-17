"""Per-harness escape-hatch overrides + model-level limits.

Covers the `[<element>.overrides.<harness>]` deep-merge (opencode + claurst), the
transparency/security warnings, and the fail-loud rules around model-level
`context_budget`/`output_limit` (required where emitted, forbidden where not)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from book_em_danno.config.generate import (
    deep_merge,
    generate,
    generate_claurst_models,
    override_warnings,
    render_config,
    resolve_limits,
)
from book_em_danno.config.schema import DannoConfig, Model


def _cfg(toml: str) -> DannoConfig:
    return DannoConfig.model_validate(tomllib.loads(toml))


# --- deep_merge (the merge primitive) -------------------------------------------------


def test_deep_merge_objects_merge_scalars_and_arrays_replace() -> None:
    base = {"a": 1, "nested": {"keep": 1, "x": "old"}, "arr": [1, 2]}
    override = {"nested": {"x": "new", "add": 2}, "arr": [9], "b": 3}
    out = deep_merge(base, override)
    # objects merge (keep survives), scalars replace, arrays replace wholesale, new keys add
    assert out == {"a": 1, "nested": {"keep": 1, "x": "new", "add": 2}, "arr": [9], "b": 3}
    # base is not mutated
    assert base["nested"] == {"keep": 1, "x": "old"}
    assert base["arr"] == [1, 2]


# --- resolve_limits -------------------------------------------------------------------


def test_resolve_limits_reads_model_level_budget() -> None:
    model = Model(backend="b", tag="t", context_budget=12345, output_limit=678)
    assert resolve_limits(model) == {"context": 12345, "output": 678}


# --- opencode overrides ---------------------------------------------------------------

_O4_MINI = """
[defaults]
default_agent = "build"

[backends.danno-openai]
kind        = "openai"
base_url    = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[backends.danno-openai.overrides.opencode]
npm = "@ai-sdk/openai"

[models.o4-mini]
backend          = "danno-openai"
tag              = "o4-mini"
context_budget   = 200000
output_limit     = 65536
reasoning_effort = "high"

[models.o4-mini.overrides.opencode.options]
max_completion_tokens = 1000000

[agents]
build = "o4-mini"
"""


def test_opencode_backend_and_model_overrides_merge(tmp_path: Path) -> None:
    import json

    cfg = _cfg(_O4_MINI)
    generate(cfg, tmp_path, apply=True)
    data = json.loads(_strip_jsonc((tmp_path / ".opencode" / "opencode.jsonc").read_text()))
    provider = data["provider"]["danno-openai"]
    # backend override replaced the hardcoded compatible SDK with the native one
    assert provider["npm"] == "@ai-sdk/openai"
    entry = provider["models"]["o4-mini"]
    # model-level limits land in the limit block
    assert entry["limit"] == {"context": 200000, "output": 65536}
    # model override deep-merged INTO options: reasoningEffort preserved, new key added
    assert entry["options"] == {"reasoningEffort": "high", "max_completion_tokens": 1000000}


def test_opencode_override_is_idempotent(tmp_path: Path) -> None:
    cfg = _cfg(_O4_MINI)
    assert generate(cfg, tmp_path, apply=True).action.value == "wrote"
    assert generate(cfg, tmp_path, apply=True).action.value == "unchanged"


def test_removing_override_reverts_generated_value(tmp_path: Path) -> None:
    import json

    generate(_cfg(_O4_MINI), tmp_path, apply=True)
    # a config without the overrides regenerates danno's own defaults
    bare = _O4_MINI.replace(
        '[backends.danno-openai.overrides.opencode]\nnpm = "@ai-sdk/openai"\n', ""
    )
    bare = bare.replace(
        "[models.o4-mini.overrides.opencode.options]\nmax_completion_tokens = 1000000\n", ""
    )
    generate(_cfg(bare), tmp_path, apply=True)
    data = json.loads(_strip_jsonc((tmp_path / ".opencode" / "opencode.jsonc").read_text()))
    provider = data["provider"]["danno-openai"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["models"]["o4-mini"]["options"] == {"reasoningEffort": "high"}


def test_opencode_agent_and_defaults_overrides() -> None:
    import json

    cfg = _cfg(
        """
[defaults]
default_agent = "build"

[defaults.overrides.opencode]
"$schema" = "https://example/custom.json"

[backends.b]
kind = "ollama"
base_url = "http://x/v1"

[models.m]
backend = "b"
tag = "t"
context_budget = 32000
output_limit = 8192

[agents.build]
model = "m"

[agents.build.overrides.opencode]
temperature = 0.2
"""
    )
    data = json.loads(_strip_jsonc(render_config(cfg)))
    # top-level (defaults) override wins
    assert data["$schema"] == "https://example/custom.json"
    # agent override merged into the agent block; `overrides` itself never leaks in
    assert data["agent"]["build"]["temperature"] == 0.2
    assert "overrides" not in data["agent"]["build"]


# --- claurst overrides ----------------------------------------------------------------


def test_claurst_backend_and_model_overrides_merge() -> None:
    cfg = _cfg(
        """
[backends.b]
kind = "ollama"
base_url = "http://x/v1"

[backends.b.overrides.claurst]
name = "Custom Ollama"

[models.m]
backend = "b"
tag = "t"
context_budget = 32000
output_limit = 8192

[models.m.overrides.claurst]
reasoning = false
"""
    )
    providers = generate_claurst_models(cfg)
    assert providers["ollama"]["name"] == "Custom Ollama"
    assert providers["ollama"]["models"]["t"]["reasoning"] is False


# --- transparency / security warnings -------------------------------------------------


def test_override_warnings_name_the_element_and_flag_sensitive_keys() -> None:
    cfg = _cfg(
        """
[backends.b]
kind = "ollama"
base_url = "http://x/v1"

[backends.b.overrides.opencode.options]
baseURL = "http://evil/v1"

[models.m]
backend = "b"
tag = "t"
context_budget = 32000
output_limit = 8192
"""
    )
    warns = override_warnings(cfg, "opencode")
    joined = "\n".join(warns)
    assert "backend 'b'" in joined
    assert "egress/auth-sensitive" in joined and "baseURL" in joined


# --- fail-loud validation -------------------------------------------------------------


def test_ollama_model_missing_limits_fails_loud() -> None:
    with pytest.raises(ValidationError, match="needs context_budget and output_limit"):
        _cfg(
            """
[backends.b]
kind = "ollama"
base_url = "http://x/v1"
[models.m]
backend = "b"
tag = "t"
"""
        )


def test_inert_model_setting_limits_fails_loud() -> None:
    with pytest.raises(ValidationError, match="meaningless on a inert backend"):
        _cfg(
            """
[backends.b]
kind = "inert"
[models.m]
backend = "b"
tag = "t"
context_budget = 1000
output_limit = 100
"""
        )


def test_inert_model_setting_overrides_fails_loud() -> None:
    with pytest.raises(ValidationError, match="sets overrides, which is meaningless"):
        _cfg(
            """
[backends.b]
kind = "inert"
[models.m]
backend = "b"
tag = "t"
[models.m.overrides.opencode]
foo = "bar"
"""
        )


def test_out_of_scope_harness_key_fails_loud() -> None:
    # `occ` is out of scope for overrides (no generated config surface) — the
    # registry-derived key set rejects it loud.
    with pytest.raises(ValidationError, match="out of scope"):
        _cfg(
            """
[backends.b]
kind = "ollama"
base_url = "http://x/v1"
[models.m]
backend = "b"
tag = "t"
context_budget = 32000
output_limit = 8192
[models.m.overrides.occ]
foo = "bar"
"""
        )


def test_defaults_claurst_override_has_no_target_fails_loud() -> None:
    with pytest.raises(ValidationError, match="has no target"):
        _cfg(
            """
[defaults.overrides.claurst]
foo = "bar"
"""
        )


def _strip_jsonc(text: str) -> str:
    """Drop `//` comment lines so a jsonc blob parses as plain JSON (danno emits comments
    only on their own lines)."""
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("//"))
