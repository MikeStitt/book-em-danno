from __future__ import annotations

import json
from pathlib import Path

import pytest

from book_em_danno.config.generate import Action, generate, render_config
from book_em_danno.config.loader import load_config
from book_em_danno.config.schema import (
    DannoConfig,
    Defaults,
    LlamacppBackend,
    Model,
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


def test_dry_run_does_not_write_first_run(tmp_path: Path) -> None:
    result = generate(_example(), tmp_path, dry_run=True)
    assert result.action is Action.DIFF
    assert not (tmp_path / ".opencode" / "opencode.jsonc").exists()


def test_llamacpp_backend_is_stubbed(tmp_path: Path) -> None:
    cfg = DannoConfig(
        defaults=Defaults(default_agent="pm"),
        backends={"local": LlamacppBackend(kind="llamacpp", base_url="http://x:8080/v1")},
        models={"m": Model(backend="local", tag="whatever")},
        agents={"pm": "m"},
    )
    with pytest.raises(NotImplementedError, match="llama.cpp"):
        generate(cfg, tmp_path)


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))
