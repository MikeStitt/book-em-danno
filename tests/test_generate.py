from __future__ import annotations

import json
from pathlib import Path

import pytest

from book_em_danno.config.generate import (
    Action,
    agent_markdown_collisions,
    generate,
    render_config,
    scan_agent_frontmatter,
)
from book_em_danno.config.loader import load_config
from book_em_danno.config.schema import (
    AgentSpec,
    DannoConfig,
    Defaults,
    LlamacppBackend,
    Model,
    NpmPlugin,
    OllamaBackend,
    OpenAIBackend,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "danno.toml.example"
MAXIMAL = Path(__file__).resolve().parent / "data" / "danno.toml.maximal.example"


def _example() -> DannoConfig:
    return load_config(EXAMPLE)


def test_render_maps_agents_to_backends() -> None:
    doc = json.loads(_strip_comments(render_config(_example())))
    assert doc["default_agent"] == "build"
    # build -> qwen3-coder-next (danno-ollama) so the top-level model is the local ref
    assert doc["model"] == "danno-ollama/qwen3-coder-next"
    assert doc["agent"]["plan"]["model"] == "danno-ollama/qwen3-coder-next"
    assert doc["agent"]["build"]["model"] == "danno-ollama/qwen3-coder-next"
    # pm uses a raw OpenCode ref (contains "/") — passed through verbatim, no provider block
    assert doc["agent"]["pm"]["model"] == "anthropic/claude-sonnet-4-6"
    assert "anthropic" not in doc.get("provider", {})
    # an ollama provider block is emitted for the local model
    assert doc["provider"]["danno-ollama"]["models"]["gemma3:27b"]["tool_call"] is False
    # provider options carry ONLY baseURL/apiKey — no inert stream/thinking/num_ctx.
    opts = doc["provider"]["danno-ollama"]["options"]
    assert set(opts) == {"baseURL", "apiKey"}
    assert doc["provider"]["danno-ollama"]["models"]["gemma3:27b"]["limit"]["output"] == 8192


def test_render_maximal_maps_raw_ref_default_and_mixed_backends() -> None:
    # The maximal fixture's default_agent (pm) maps to a raw inline OpenCode ref, so
    # the top-level `model` renders as that ref verbatim with no provider block — the
    # cloud-as-default path after retiring the `cloud` backend. Agents also span
    # ollama and nvidia (openai-compatible) backends.
    cfg = load_config(MAXIMAL)
    doc = json.loads(_strip_comments(render_config(cfg)))
    assert doc["default_agent"] == "pm"
    assert doc["model"] == "anthropic/claude-sonnet-4-6"  # raw ref, not a <prov>/<tag> ref
    assert doc["agent"]["pm"]["model"] == "anthropic/claude-sonnet-4-6"
    assert doc["agent"]["build"]["model"] == "ollama/gemma3:27b"
    assert doc["agent"]["research"]["model"] == "nvidia/nvidia/nemotron-3-ultra-550b-a55b"
    # a raw ref gets no provider block; the nvidia (openai-compatible) one still does
    assert "anthropic" not in doc["provider"]
    assert doc["provider"]["nvidia"]["options"]["apiKey"] == "{env:NVIDIA_API_KEY}"


def test_generate_maximal_writes_despite_unused_llamacpp(tmp_path: Path) -> None:
    # The fixture declares a llamacpp backend but no model uses it, so the stub
    # must not fire — generate() writes the config.
    result = generate(load_config(MAXIMAL), tmp_path)
    assert result.action is Action.WROTE
    assert (tmp_path / ".opencode" / "opencode.jsonc").is_file()


def test_all_defined_ollama_models_emitted_even_when_unassigned() -> None:
    # The whole catalog must reach opencode's picker, not just agent-assigned models.
    cfg = DannoConfig(
        defaults=Defaults(default_agent="pm"),
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1")
        },
        models={
            "assigned": Model(backend="ollama", tag="gemma3:27b", tool_call=True),
            "spare": Model(backend="ollama", tag="qwen3-coder-next", tool_call=True),
        },
        agents={"pm": "assigned"},  # 'spare' is defined but unassigned
    )
    models = json.loads(_strip_comments(render_config(cfg)))["provider"]["ollama"]["models"]
    assert set(models) == {"gemma3:27b", "qwen3-coder-next"}


def test_no_inert_runtime_keys_anywhere() -> None:
    # The verified-inert keys (provider-level stream/thinking, body num_ctx) must
    # not appear in the emitted JSON — they never reach Ollama. (The header comment
    # names them while explaining their absence, so check the comment-stripped body.)
    body = _strip_comments(render_config(_example()))
    for inert in ("stream", "thinking", "num_ctx"):
        assert inert not in body, f"{inert!r} should not be emitted"


def test_ollama_context_and_output_budget() -> None:
    cfg = DannoConfig(
        defaults=Defaults(default_agent="pm"),
        backends={
            "ollama": OllamaBackend(
                kind="ollama",
                base_url="http://host.docker.internal:11434/v1",
                context_budget=262144,
                output_limit=4096,
            )
        },
        models={"gemma": Model(backend="ollama", tag="gemma4:26b", tool_call=True)},
        agents={"pm": "gemma"},
    )
    doc = json.loads(_strip_comments(render_config(cfg)))
    limit = doc["provider"]["ollama"]["models"]["gemma4:26b"]["limit"]
    assert limit["context"] == 262144
    assert limit["output"] == 4096
    # No reasoning_effort configured -> no model-level options block.
    assert "options" not in doc["provider"]["ollama"]["models"]["gemma4:26b"]


def test_reasoning_effort_emitted_as_camelcase_when_set() -> None:
    cfg = DannoConfig(
        defaults=Defaults(default_agent="pm"),
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1")
        },
        models={
            "gemma": Model(
                backend="ollama", tag="gemma4:26b", tool_call=True, reasoning_effort="none"
            )
        },
        agents={"pm": "gemma"},
    )
    rendered = render_config(cfg)
    # camelCase is load-bearing (see generate.py); snake_case would be clobbered.
    assert "reasoningEffort" in rendered
    assert "reasoning_effort" not in rendered
    doc = json.loads(_strip_comments(rendered))
    assert doc["provider"]["ollama"]["models"]["gemma4:26b"]["options"] == {
        "reasoningEffort": "none"
    }


def test_openai_backend_emits_env_substituted_api_key() -> None:
    # A generic OpenAI-compatible backend (e.g. NVIDIA NIM) renders an
    # @ai-sdk/openai-compatible provider with apiKey via {env:VAR} — the secret is
    # never written into the config. The model ref is <provider>/<tag>.
    cfg = DannoConfig(
        defaults=Defaults(default_agent="plan"),
        backends={
            "nvidia": OpenAIBackend(
                kind="openai",
                base_url="https://integrate.api.nvidia.com/v1",
                api_key_env="NVIDIA_API_KEY",
                context_budget=128000,
            )
        },
        models={
            "nemotron": Model(
                backend="nvidia", tag="nvidia/nemotron-3-ultra-550b-a55b", tool_call=True
            )
        },
        agents={"plan": "nemotron"},
    )
    doc = json.loads(_strip_comments(render_config(cfg)))
    prov = doc["provider"]["nvidia"]
    assert prov["npm"] == "@ai-sdk/openai-compatible"
    assert prov["options"]["baseURL"] == "https://integrate.api.nvidia.com/v1"
    assert prov["options"]["apiKey"] == "{env:NVIDIA_API_KEY}"  # no literal secret
    model = prov["models"]["nvidia/nemotron-3-ultra-550b-a55b"]
    assert model["tool_call"] is True
    assert model["limit"]["context"] == 128000
    assert doc["agent"]["plan"]["model"] == "nvidia/nvidia/nemotron-3-ultra-550b-a55b"


def test_first_run_writes(tmp_path: Path) -> None:
    result = generate(_example(), tmp_path)
    assert result.action is Action.WROTE
    assert (tmp_path / ".opencode" / "opencode.jsonc").is_file()


def test_rerun_is_noop(tmp_path: Path) -> None:
    generate(_example(), tmp_path)
    second = generate(_example(), tmp_path)
    assert second.action is Action.UNCHANGED


def test_change_requires_apply(tmp_path: Path) -> None:
    generate(_example(), tmp_path)
    dest = tmp_path / ".opencode" / "opencode.jsonc"
    dest.write_text(dest.read_text(encoding="utf-8") + "// hand edit\n", encoding="utf-8")

    # Without --apply: a diff is returned and the file is left untouched.
    diffed = generate(_example(), tmp_path)
    assert diffed.action is Action.DIFF
    assert diffed.diff
    assert dest.read_text(encoding="utf-8").endswith("// hand edit\n")

    # With --apply: the file is overwritten with the generated content.
    applied = generate(_example(), tmp_path, apply=True)
    assert applied.action is Action.WROTE
    assert not dest.read_text(encoding="utf-8").endswith("// hand edit\n")


def test_llamacpp_backend_is_stubbed(tmp_path: Path) -> None:
    cfg = DannoConfig(
        defaults=Defaults(default_agent="pm"),
        backends={"local": LlamacppBackend(kind="llamacpp", base_url="http://x:8080/v1")},
        models={"m": Model(backend="local", tag="whatever")},
        agents={"pm": "m"},
    )
    with pytest.raises(NotImplementedError, match="llama.cpp"):
        generate(cfg, tmp_path)


def _npm_config(plugins: list[NpmPlugin]) -> DannoConfig:
    return DannoConfig(
        defaults=Defaults(default_agent="pm"),
        agents={"pm": "anthropic/claude-sonnet-4-6"},  # raw inline ref — no backend/[models]
        npm=plugins,
    )


def test_npm_plugins_render_plugin_array() -> None:
    cfg = _npm_config(
        [
            NpmPlugin(package="opencode-planner"),
            NpmPlugin(package="@plannotator/opencode@latest", config={"workflow": "plan-agent"}),
        ]
    )
    doc = json.loads(_strip_comments(render_config(cfg)))
    # bare string for a config-less plugin; [package, config] tuple otherwise.
    assert doc["plugin"] == [
        "opencode-planner",
        ["@plannotator/opencode@latest", {"workflow": "plan-agent"}],
    ]


def test_no_plugin_key_when_npm_empty() -> None:
    doc = json.loads(_strip_comments(render_config(_npm_config([]))))
    assert "plugin" not in doc


# --- rich [agents] AgentSpec form ------------------------------------------


def _ollama_cfg(agents: dict, **kw: object) -> DannoConfig:
    return DannoConfig(
        defaults=Defaults(default_agent="pm"),
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1")
        },
        models={"local": Model(backend="ollama", tag="qwen3-coder-next", tool_call=True)},
        agents=agents,
        **kw,  # type: ignore[arg-type]
    )


def test_rich_agent_emits_passthrough_fields_with_resolved_model() -> None:
    cfg = _ollama_cfg(
        {
            "pm": "local",
            "architect": AgentSpec(
                model="anthropic/claude-sonnet-4-6",
                mode="subagent",
                temperature=0.1,
                permission={"edit": "deny"},
            ),
        }
    )
    agent = json.loads(_strip_comments(render_config(cfg)))["agent"]["architect"]
    assert agent["model"] == "anthropic/claude-sonnet-4-6"  # raw ref passed through
    assert agent["mode"] == "subagent"
    assert agent["temperature"] == 0.1
    assert agent["permission"] == {"edit": "deny"}


def test_rich_agent_resolves_a_bare_model_name() -> None:
    # A rich agent can route a built-in subagent to a local [models] entry.
    cfg = _ollama_cfg({"pm": "local", "explore": AgentSpec(model="local")})
    agent = json.loads(_strip_comments(render_config(cfg)))["agent"]["explore"]
    assert agent["model"] == "ollama/qwen3-coder-next"  # resolved via [models]


def test_rich_agent_without_model_omits_the_model_key() -> None:
    # An agent that only pins behavior (no model) emits no `model` key — OpenCode /
    # a markdown def supplies it. The top-level model falls back to a declared model.
    cfg = _ollama_cfg({"pm": "local", "locked": AgentSpec(permission={"edit": "deny"})})
    doc = json.loads(_strip_comments(render_config(cfg)))
    assert "model" not in doc["agent"]["locked"]
    assert doc["agent"]["locked"]["permission"] == {"edit": "deny"}
    assert doc["model"] == "ollama/qwen3-coder-next"  # default_agent pm -> local


def test_default_agent_rich_spec_drives_top_level_model() -> None:
    cfg = _ollama_cfg({"pm": AgentSpec(model="anthropic/claude-sonnet-4-6", mode="primary")})
    doc = json.loads(_strip_comments(render_config(cfg)))
    assert doc["model"] == "anthropic/claude-sonnet-4-6"


# --- markdown collision warnings -------------------------------------------


def _write_agent_md(target: Path, name: str, frontmatter: str, *, singular: bool = True) -> None:
    sub = "agent" if singular else "agents"
    d = target / ".opencode" / sub
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\n{frontmatter}---\nbody is the prompt\n", encoding="utf-8")


def test_scan_reads_frontmatter_keys_from_both_dirs(tmp_path: Path) -> None:
    _write_agent_md(tmp_path, "reviewer", "mode: subagent\ntemperature: 0.2\n", singular=True)
    _write_agent_md(tmp_path, "planner", "description: plan\n", singular=False)
    found = scan_agent_frontmatter(tmp_path)
    assert found["reviewer"] == {"mode", "temperature"}
    assert found["planner"] == {"description"}


def test_scan_skips_nested_keys(tmp_path: Path) -> None:
    # Indented keys belong to a parent map (e.g. permission:) — not top-level fields.
    _write_agent_md(tmp_path, "r", "permission:\n  edit: deny\nmode: subagent\n")
    assert scan_agent_frontmatter(tmp_path)["r"] == {"permission", "mode"}


def test_collision_warns_when_markdown_sets_the_same_field(tmp_path: Path) -> None:
    _write_agent_md(tmp_path, "reviewer", "model: gpt-oss:20b\ntemperature: 0.9\n")
    cfg = _ollama_cfg(
        {"pm": "local", "reviewer": AgentSpec(model="local", temperature=0.1, mode="subagent")}
    )
    result = generate(cfg, tmp_path)
    assert len(result.warnings) == 1
    msg = result.warnings[0]
    # only the overlapping fields are listed (sorted); mode is danno-only, not shadowed.
    assert "reviewer" in msg and "both set model, temperature —" in msg


def test_no_collision_when_fields_are_disjoint(tmp_path: Path) -> None:
    _write_agent_md(tmp_path, "reviewer", "description: reviews code\n")
    cfg = _ollama_cfg({"pm": "local", "reviewer": AgentSpec(model="local", temperature=0.1)})
    assert generate(cfg, tmp_path).warnings == []


def test_markdown_body_shadows_a_danno_prompt(tmp_path: Path) -> None:
    # `prompt` never appears in frontmatter (the body IS the prompt), but a markdown
    # file still shadows a danno-set prompt — special-cased so it's not missed.
    _write_agent_md(tmp_path, "writer", "mode: subagent\n")
    cfg = _ollama_cfg({"pm": "local", "writer": AgentSpec(model="local", prompt="{file:./p.md}")})
    warnings = generate(cfg, tmp_path).warnings
    assert len(warnings) == 1 and "prompt" in warnings[0]


def test_string_shorthand_collides_only_on_model(tmp_path: Path) -> None:
    # The string form sets only `model`; a markdown `model:` is the classic shadow.
    _write_agent_md(tmp_path, "pm", "model: gpt-oss:20b\n")
    cfg = _ollama_cfg({"pm": "local"})
    warnings = generate(cfg, tmp_path).warnings
    assert len(warnings) == 1 and "model" in warnings[0]


def test_no_warnings_without_any_markdown(tmp_path: Path) -> None:
    cfg = _ollama_cfg({"pm": AgentSpec(model="local", temperature=0.1, prompt="x")})
    assert agent_markdown_collisions(cfg, scan_agent_frontmatter(tmp_path)) == []


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
