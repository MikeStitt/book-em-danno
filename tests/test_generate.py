from __future__ import annotations

import json
from pathlib import Path

import pytest

from book_em_danno.config.generate import (
    Action,
    agent_markdown_collisions,
    claurst_agent_ref,
    claurst_agent_unmapped_warnings,
    claurst_model_ref,
    claurst_provider_id,
    generate,
    generate_claurst,
    generate_claurst_agents,
    generate_claurst_models,
    generate_md,
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
    assert doc["provider"]["danno-ollama"]["models"]["qwen3-coder-next"]["tool_call"] is True
    # provider options carry ONLY baseURL/apiKey — no inert stream/thinking/num_ctx.
    opts = doc["provider"]["danno-ollama"]["options"]
    assert set(opts) == {"baseURL", "apiKey"}
    assert doc["provider"]["danno-ollama"]["models"]["qwen3-coder-next"]["limit"]["output"] == 8192


def test_disable_title_emits_title_pseudo_agent_only_when_requested() -> None:
    # Off by default (what `danno install` writes): no `title` entry, so opencode's
    # per-session thread-title generator stays on for real user projects.
    default_doc = json.loads(_strip_comments(render_config(_example())))
    assert "title" not in default_doc["agent"]
    # On for the validator sweep path: emits opencode's `agent.title.disable` to stop
    # the title call (verified on the wire to otherwise hit the local default model).
    disabled_doc = json.loads(_strip_comments(render_config(_example(), disable_title=True)))
    assert disabled_doc["agent"]["title"] == {"disable": True}
    # The real agents are untouched alongside the injected pseudo-agent.
    assert disabled_doc["agent"]["build"]["model"] == "danno-ollama/qwen3-coder-next"


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
            "assigned": Model(backend="ollama", tag="gemma3:27b"),
            "spare": Model(backend="ollama", tag="qwen3-coder-next"),
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
        models={"gemma": Model(backend="ollama", tag="gemma4:26b")},
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
        models={"gemma": Model(backend="ollama", tag="gemma4:26b", reasoning_effort="none")},
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
        models={"nemotron": Model(backend="nvidia", tag="nvidia/nemotron-3-ultra-550b-a55b")},
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


def _claurst_cfg() -> DannoConfig:
    """A mixed ollama + NVIDIA-NIM config for the claurst-overlay tests."""
    return DannoConfig(
        defaults=Defaults(default_agent="build"),
        backends={
            "danno-ollama": OllamaBackend(
                kind="ollama",
                base_url="http://host.docker.internal:11434/v1",
                context_budget=65536,
                output_limit=8192,
            ),
            "danno-nvidia": OpenAIBackend(
                kind="openai",
                base_url="https://integrate.api.nvidia.com/v1",
                api_key_env="NVIDIA_API_KEY",
                context_budget=262144,
                output_limit=32768,
            ),
        },
        models={
            "coder": Model(backend="danno-ollama", tag="qwen3-coder-next", reasoning_effort="none"),
            "glm": Model(backend="danno-nvidia", tag="z-ai/glm-5.1", reasoning_effort="medium"),
        },
        agents={"build": "coder"},
    )


def test_generate_claurst_models_overlay_shape() -> None:
    overlay = generate_claurst_models(_claurst_cfg())
    # Grouped by claurst PROVIDER id (ollama/nvidia), not the danno backend name.
    assert set(overlay) == {"ollama", "nvidia"}
    assert overlay["ollama"]["id"] == "ollama"
    assert overlay["nvidia"]["name"] == "Nvidia"
    # Model keyed by tag, with tool_call=true (Bug 7) and danno's window (Bug 4).
    ollama_entry = overlay["ollama"]["models"]["qwen3-coder-next"]
    assert ollama_entry["tool_call"] is True
    assert ollama_entry["limit"] == {"context": 65536, "output": 8192}
    nvidia_entry = overlay["nvidia"]["models"]["z-ai/glm-5.1"]
    assert nvidia_entry["tool_call"] is True
    assert nvidia_entry["limit"] == {"context": 262144, "output": 32768}


def test_generate_claurst_models_reasoning_flag() -> None:
    overlay = generate_claurst_models(_claurst_cfg())
    # reasoning_effort="none" -> not a reasoning model; "medium" -> reasoning=true.
    assert "reasoning" not in overlay["ollama"]["models"]["qwen3-coder-next"]
    assert overlay["nvidia"]["models"]["z-ai/glm-5.1"]["reasoning"] is True


def test_claurst_model_ref_uses_claurst_provider() -> None:
    cfg = _claurst_cfg()
    # provider/tag, where provider is claurst's (NOT the danno backend name).
    assert claurst_model_ref(cfg, "coder") == "ollama/qwen3-coder-next"
    assert claurst_model_ref(cfg, "glm") == "nvidia/z-ai/glm-5.1"
    assert claurst_provider_id(cfg, "coder") == "ollama"
    assert claurst_provider_id(cfg, "glm") == "nvidia"


def test_claurst_models_unmapped_openai_host_raises() -> None:
    cfg = DannoConfig(
        defaults=Defaults(default_agent="build"),
        backends={
            "other": OpenAIBackend(
                kind="openai", base_url="https://api.example.com/v1", api_key_env="X_KEY"
            )
        },
        models={"m": Model(backend="other", tag="some-model")},
        agents={"build": "m"},
    )
    with pytest.raises(NotImplementedError, match="no claurst provider mapping"):
        generate_claurst_models(cfg)


def test_claurst_models_llamacpp_is_stubbed() -> None:
    cfg = DannoConfig(
        defaults=Defaults(default_agent="build"),
        backends={"lc": LlamacppBackend(kind="llamacpp", base_url="http://x:8080/v1")},
        models={"m": Model(backend="lc", tag="m")},
        agents={"build": "m"},
    )
    with pytest.raises(NotImplementedError):
        generate_claurst_models(cfg)


def _claurst_agents_cfg(agents: dict[str, object]) -> DannoConfig:
    """`_claurst_cfg`'s backends/models with a caller-supplied [agents] map."""
    base = _claurst_cfg()
    return DannoConfig(
        defaults=Defaults(default_agent="build"),
        backends=base.backends,
        models=base.models,
        agents=agents,  # type: ignore[arg-type]
    )


def test_generate_claurst_agents_string_shorthand() -> None:
    agents = generate_claurst_agents(_claurst_cfg())
    # `build = "coder"` -> a claurst agent pinning the resolved provider/tag model.
    assert agents == {"build": {"model": "ollama/qwen3-coder-next"}}


def test_generate_claurst_agents_rich_field_map() -> None:
    cfg = _claurst_agents_cfg(
        {
            "review": AgentSpec(
                model="glm",
                description="reviewer",
                prompt="be strict",
                temperature=0.3,
                steps=30,
                color="magenta",
                hidden=True,
            )
        }
    )
    agents = generate_claurst_agents(cfg)
    assert agents["review"] == {
        "model": "nvidia/z-ai/glm-5.1",  # resolved via claurst provider
        "description": "reviewer",
        "prompt": "be strict",
        "temperature": 0.3,
        "max_turns": 30,  # steps -> max_turns (Bug-2 turn lever)
        "color": "magenta",
        "visible": False,  # hidden -> visible inverted
    }


def test_claurst_agent_ref_passthrough_vs_models_entry() -> None:
    cfg = _claurst_cfg()
    # A '/'-bearing value is a raw claurst ref; a bare name resolves a [models] entry.
    assert claurst_agent_ref(cfg, "anthropic/claude-opus-4-6") == "anthropic/claude-opus-4-6"
    assert claurst_agent_ref(cfg, "coder") == "ollama/qwen3-coder-next"


def test_generate_claurst_agents_drops_empty_and_warns_unmapped() -> None:
    cfg = _claurst_agents_cfg(
        {"sub": AgentSpec(mode="subagent", top_p=0.9, permission={"edit": "deny"})}
    )
    # Every set field is unmapped -> the agent maps to nothing -> dropped.
    assert generate_claurst_agents(cfg) == {}
    warnings = claurst_agent_unmapped_warnings(cfg)
    assert len(warnings) == 1
    assert "mode, permission, top_p" in warnings[0]
    assert "claurst" in warnings[0]


def test_claurst_agent_unmapped_warnings_silent_when_clean() -> None:
    # String shorthand and fully-mappable rich agents produce no warnings.
    cfg = _claurst_agents_cfg(
        {"build": "coder", "r": AgentSpec(model="glm", steps=10, color="cyan")}
    )
    assert claurst_agent_unmapped_warnings(cfg) == []


def test_generate_claurst_writes_overlay_and_settings(tmp_path: Path) -> None:
    cdir = tmp_path / ".claurst"
    results = generate_claurst(_claurst_cfg(), cdir, apply=True)
    # Fresh dir: both files written (nothing to clobber -> no --apply needed).
    assert {r.action for r in results} == {Action.WROTE}
    models = json.loads((cdir / "models.json").read_text())
    assert models["ollama"]["models"]["qwen3-coder-next"]["tool_call"] is True
    settings = json.loads((cdir / "settings.json").read_text())
    assert settings["agents"] == {"build": {"model": "ollama/qwen3-coder-next"}}


def test_generate_claurst_settings_preserves_other_keys(tmp_path: Path) -> None:
    cdir = tmp_path / ".claurst"
    cdir.mkdir()
    # A user's existing settings.json with their own keys and a stale agents map.
    (cdir / "settings.json").write_text(
        json.dumps({"theme": "dark", "agents": {"old": {"model": "x"}}})
    )
    generate_claurst(_claurst_cfg(), cdir, apply=True)
    settings = json.loads((cdir / "settings.json").read_text())
    assert settings["theme"] == "dark"  # user key preserved
    assert "old" not in settings["agents"]  # danno owns the agents key wholesale
    assert settings["agents"] == {"build": {"model": "ollama/qwen3-coder-next"}}


def test_generate_claurst_diff_without_apply(tmp_path: Path) -> None:
    cdir = tmp_path / ".claurst"
    cdir.mkdir()
    (cdir / "models.json").write_text(json.dumps({"stale": True}))
    (cdir / "settings.json").write_text(json.dumps({"agents": {}}))
    results = generate_claurst(_claurst_cfg(), cdir, apply=False)
    # Existing files that would change are advised (DIFF), not written.
    assert {r.action for r in results} == {Action.DIFF}
    assert json.loads((cdir / "models.json").read_text()) == {"stale": True}


def test_generate_claurst_warns_unmapped_on_settings_result(tmp_path: Path) -> None:
    cfg = _claurst_agents_cfg({"sub": AgentSpec(mode="subagent")})
    results = generate_claurst(cfg, tmp_path / ".claurst", apply=True)
    settings_result = next(r for r in results if r.path.name == "settings.json")
    assert any("mode" in w and "claurst" in w for w in settings_result.warnings)


def test_first_run_writes(tmp_path: Path) -> None:
    result = generate(_example(), tmp_path)
    assert result.action is Action.WROTE
    assert (tmp_path / ".opencode" / "opencode.jsonc").is_file()


def test_rerun_is_noop(tmp_path: Path) -> None:
    generate(_example(), tmp_path)
    second = generate(_example(), tmp_path)
    assert second.action is Action.UNCHANGED


def test_in_region_change_requires_apply(tmp_path: Path) -> None:
    # An edit INSIDE danno's managed region is reasserted — but only under --apply.
    generate(_example(), tmp_path)
    dest = tmp_path / ".opencode" / "opencode.jsonc"
    tampered = dest.read_text(encoding="utf-8").replace(
        "danno-ollama/qwen3-coder-next", "danno-ollama/TAMPERED"
    )
    assert "TAMPERED" in tampered
    dest.write_text(tampered, encoding="utf-8")

    # Without --apply: a diff is returned and the file is left untouched.
    diffed = generate(_example(), tmp_path)
    assert diffed.action is Action.DIFF
    assert diffed.diff
    assert "TAMPERED" in dest.read_text(encoding="utf-8")

    # With --apply: danno's managed region is restored.
    applied = generate(_example(), tmp_path, apply=True)
    assert applied.action is Action.WROTE
    assert "TAMPERED" not in dest.read_text(encoding="utf-8")


def test_hand_edit_outside_region_is_preserved() -> None:
    # The whole point of managed-region merge: a user key + comment OUTSIDE the markers
    # survives verbatim, danno's region is refreshed, and the splice stays valid (the
    # last danno member gains the comma a following user key needs). Idempotent.
    from book_em_danno.config.generate import (
        _JSONC_BEGIN,
        _JSONC_END,
        _merge_jsonc,
        _region_inner,
    )

    region = _region_inner(_ollama_cfg({"pm": "local"}), frozenset())
    existing = (
        "{\n"
        f"  {_JSONC_BEGIN}\n"
        '  "stale": "danno-owned",\n'
        f"  {_JSONC_END}\n"
        '  "userKey": 7  // mine\n'
        "}\n"
    )
    merged = _merge_jsonc(existing, region)
    assert '"userKey": 7  // mine' in merged  # user content preserved verbatim
    assert '"stale"' not in merged  # danno region replaced
    assert '"$schema"' in merged  # danno region present
    lines = merged.splitlines()
    end_idx = next(i for i, ln in enumerate(lines) if _JSONC_END in ln)
    assert lines[end_idx - 1].rstrip().endswith(",")  # last danno member got its comma
    assert _merge_jsonc(merged, region) == merged  # idempotent


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
        models={"local": Model(backend="ollama", tag="qwen3-coder-next")},
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
    # model is routed into the md (not shadowed); mode is danno-only; only the
    # non-routed shadowed field (temperature) is warned.
    assert "reviewer" in msg and "both set temperature —" in msg
    assert "model" not in msg


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


def test_model_routed_to_md_not_warned(tmp_path: Path) -> None:
    # When an md controls the agent, danno ROUTES model into the md (where it wins)
    # instead of warning — and omits it from the jsonc agent block.
    _write_agent_md(tmp_path, "pm", "model: gpt-oss:20b\n")
    cfg = _ollama_cfg({"pm": "local"})
    result = generate(cfg, tmp_path)
    assert result.warnings == []  # model is routed, not shadowed
    doc = json.loads(_strip_comments(result.content))
    assert "pm" not in doc.get("agent", {})  # model-only agent dropped from jsonc


def test_no_warnings_without_any_markdown(tmp_path: Path) -> None:
    cfg = _ollama_cfg({"pm": AgentSpec(model="local", temperature=0.1, prompt="x")})
    assert agent_markdown_collisions(cfg, scan_agent_frontmatter(tmp_path)) == []


# --- md frontmatter routing (generate_md) ----------------------------------


def test_jsonc_first_write_has_markers(tmp_path: Path) -> None:
    from book_em_danno.config.generate import _JSONC_BEGIN, _JSONC_END

    result = generate(_ollama_cfg({"pm": "local"}), tmp_path)
    assert _JSONC_BEGIN in result.content and _JSONC_END in result.content


def test_generate_md_routes_model_into_frontmatter(tmp_path: Path) -> None:
    _write_agent_md(tmp_path, "reviewer", "description: reviews\nmode: subagent\n")
    cfg = _ollama_cfg({"pm": "local", "reviewer": "local"})
    results = generate_md(cfg, tmp_path, apply=True)
    md = (tmp_path / ".opencode" / "agent" / "reviewer.md").read_text(encoding="utf-8")
    assert "model: ollama/qwen3-coder-next" in md  # routed model written
    assert "description: reviews" in md and "mode: subagent" in md  # other frontmatter kept
    assert "body is the prompt" in md  # body preserved verbatim
    assert any(r.action is Action.WROTE and r.path.name == "reviewer.md" for r in results)
    # the jsonc side omits the routed model for that agent
    doc = json.loads(_strip_comments(generate(cfg, tmp_path).content))
    assert "reviewer" not in doc.get("agent", {})


def test_generate_md_idempotent(tmp_path: Path) -> None:
    _write_agent_md(tmp_path, "reviewer", "mode: subagent\n")
    cfg = _ollama_cfg({"pm": "local", "reviewer": "local"})
    generate_md(cfg, tmp_path, apply=True)
    again = generate_md(cfg, tmp_path, apply=True)
    assert again and all(r.action is Action.UNCHANGED for r in again)


def test_generate_md_creates_frontmatter_when_absent(tmp_path: Path) -> None:
    d = tmp_path / ".opencode" / "agent"
    d.mkdir(parents=True)
    (d / "writer.md").write_text("Just a body prompt, no frontmatter.\n", encoding="utf-8")
    cfg = _ollama_cfg({"pm": "local", "writer": "local"})
    generate_md(cfg, tmp_path, apply=True)
    md = (d / "writer.md").read_text(encoding="utf-8")
    assert md.startswith("---\n")  # a frontmatter fence was created
    assert "model: ollama/qwen3-coder-next" in md
    assert "Just a body prompt, no frontmatter." in md  # body preserved


def test_merge_md_preserves_body_and_other_keys() -> None:
    from book_em_danno.config.generate import _MD_BEGIN, _MD_END, _merge_md

    existing = "---\ndescription: x\nmode: subagent\n---\nThe body.\n"
    merged = _merge_md(existing, ["model: ollama/foo"])
    assert "description: x" in merged and "mode: subagent" in merged and "The body." in merged
    assert "model: ollama/foo" in merged and _MD_BEGIN in merged and _MD_END in merged
    assert _merge_md(merged, ["model: ollama/foo"]) == merged  # idempotent
    changed = _merge_md(merged, ["model: ollama/bar"])  # in-place value update
    assert "model: ollama/bar" in changed and "model: ollama/foo" not in changed
    assert "The body." in changed  # body still intact


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
