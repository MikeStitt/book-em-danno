from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.commands import install, ollama, sandbox
from book_em_danno.config.schema import (
    CloudBackend,
    DannoConfig,
    Model,
    NpmPlugin,
    OllamaBackend,
)
from book_em_danno.core import registry
from conftest import RecordingRunner


def _config() -> DannoConfig:
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1"),
            "cloud": CloudBackend(kind="cloud", provider="anthropic"),
        },
        models={
            "gemma": Model(backend="ollama", tag="gemma4:26b", tool_call=True),
            "sonnet": Model(backend="cloud", id="anthropic/claude-sonnet-4-6"),
        },
        agents={"pm": "sonnet", "runner": "gemma"},
        npm=[
            NpmPlugin(
                package="@plannotator/opencode@latest",
                setup=["curl -fsSL https://plannotator.ai/install.sh | bash"],
            )
        ],
    )


def test_install_orchestration_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "loopback_warning", lambda **k: None)
    # Keep agent-home + registry off real host state (tmp_path has no danno.toml,
    # so the default per-project home is mounted — same as `sandbox start`).
    home_root = tmp_path / "agent-home"
    monkeypatch.setattr(sandbox, "_agent_home_root", lambda: home_root)
    monkeypatch.setattr(registry, "default_path", lambda: tmp_path / "sandboxes.json")
    r = RecordingRunner()
    install.run_install(_config(), tmp_path, r)
    name = f"danno-{tmp_path.parent.name}-{tmp_path.name}"
    home = home_root / name
    assert r.joined() == [
        "ollama pull gemma4:26b",  # step 2: models
        # step 3 (tools) is empty here — the plugin is declarative [[npm]], not a tool
        f"mkdir -p {home}",  # step 4: ensure the agent-home mount source exists
        f"docker sandbox create --name {name} opencode {tmp_path} {home}",  # 2-mount create
        f"docker sandbox network proxy {name} --policy allow --allow-host localhost:11434",
        f"docker sandbox stop {name}",
        # post-create: the [[npm]] plugin's in-container setup runs via exec
        f"docker sandbox exec {name} bash -lc curl -fsSL https://plannotator.ai/install.sh | bash",
    ]


def test_ollama_tags_deduped_and_only_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two agents pointing at the same ollama model yield one pull; cloud excluded.
    cfg = _config()
    cfg.agents["committer"] = "gemma"
    assert install._ollama_tags(cfg) == ["gemma4:26b"]


def test_ollama_tags_includes_unassigned_models() -> None:
    # Every defined ollama model is pulled, even if no agent references it.
    cfg = _config()
    cfg.models["spare"] = Model(backend="ollama", tag="qwen3-coder-next", tool_call=True)
    assert install._ollama_tags(cfg) == ["gemma4:26b", "qwen3-coder-next"]


def test_install_missing_target_fails_loud() -> None:
    r = RecordingRunner()
    with pytest.raises(install.InstallError):
        install.run_install(_config(), Path("/no/such/dir"), r)
