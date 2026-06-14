from __future__ import annotations

import json
from pathlib import Path

import pytest

from book_em_danno.config.generate import Action, generate, render_config
from book_em_danno.config.loader import load_config
from book_em_danno.config.schema import (
    CloudBackend,
    DannoConfig,
    Defaults,
    LlamacppBackend,
    Model,
    NpmPlugin,
    OllamaBackend,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "danno.toml.example"


def _example() -> DannoConfig:
    return load_config(EXAMPLE)


def test_render_maps_agents_to_backends() -> None:
    doc = json.loads(_strip_comments(render_config(_example())))
    assert doc["default_agent"] == "pm"
    # pm -> sonnet (cloud) so the top-level model is the cloud id
    assert doc["model"] == "anthropic/claude-sonnet-4-6"
    assert doc["agent"]["architect"]["model"] == "anthropic/claude-sonnet-4-6"
    assert doc["agent"]["committer"]["model"] == "ollama/gemma3:27b"
    # an ollama provider block is emitted for the local model
    assert doc["provider"]["ollama"]["models"]["gemma3:27b"]["tool_call"] is True
    # provider options carry ONLY baseURL/apiKey — no inert stream/thinking/num_ctx.
    opts = doc["provider"]["ollama"]["options"]
    assert set(opts) == {"baseURL", "apiKey"}
    assert doc["provider"]["ollama"]["models"]["gemma3:27b"]["limit"]["output"] == 8192


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
        backends={"cloud": CloudBackend(kind="cloud", provider="anthropic")},
        models={"sonnet": Model(backend="cloud", id="anthropic/claude-sonnet-4-6")},
        agents={"pm": "sonnet"},
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


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
