from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.commands import install, ollama, sandbox, tools
from book_em_danno.config.schema import (
    DannoConfig,
    Model,
    NpmPlugin,
    OllamaBackend,
    Tool,
)
from book_em_danno.core import registry
from conftest import RecordingRunner


def _config() -> DannoConfig:
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1"),
        },
        models={
            "gemma": Model(
                backend="ollama", tag="gemma4:26b", context_budget=32000, output_limit=8192
            ),
        },
        agents={"pm": "anthropic/claude-sonnet-4-6", "runner": "gemma"},  # pm = raw inline ref
        npm=[
            NpmPlugin(
                package="@plannotator/opencode@latest",
                setup=["curl -fsSL https://plannotator.ai/install.sh | bash"],
            )
        ],
    )


def test_install_orchestration_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "lan_exposure_warning", lambda **k: None)
    # Deterministic: pretend no models are present so every tag is pulled.
    monkeypatch.setattr(ollama, "installed_tags", lambda **k: set())
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
    # Two agents pointing at the same ollama model yield one pull; the raw-ref
    # (cloud) agent contributes no tag.
    cfg = _config()
    cfg.agents["committer"] = "gemma"
    assert install._ollama_tags(cfg) == ["gemma4:26b"]


def test_ollama_tags_includes_unassigned_models() -> None:
    # Every defined ollama model is pulled, even if no agent references it.
    cfg = _config()
    cfg.models["spare"] = Model(
        backend="ollama", tag="qwen3-coder-next", context_budget=32000, output_limit=8192
    )
    assert install._ollama_tags(cfg) == ["gemma4:26b", "qwen3-coder-next"]


def test_install_skips_already_present_ollama_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A model already in `ollama list` is not re-pulled; an absent one is.
    monkeypatch.setattr(ollama, "lan_exposure_warning", lambda **k: None)
    monkeypatch.setattr(sandbox, "_agent_home_root", lambda: tmp_path / "agent-home")
    monkeypatch.setattr(registry, "default_path", lambda: tmp_path / "sandboxes.json")
    # gemma4:26b is the only defined ollama model; mark it present (bare tag → :latest
    # normalization is exercised by adding a second model below).
    monkeypatch.setattr(ollama, "installed_tags", lambda **k: {"gemma4:26b"})
    cfg = _config()
    cfg.models["spare"] = Model(
        backend="ollama", tag="spare-model", context_budget=32000, output_limit=8192
    )
    r = RecordingRunner()
    install.run_install(cfg, tmp_path, r)
    pulls = [c for c in r.joined() if c.startswith("ollama pull")]
    assert pulls == ["ollama pull spare-model"]  # gemma4:26b present → skipped


def test_install_fails_loud_when_a_tool_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed tool installer must raise (no silent "ready") and must abort before
    # the sandbox is provisioned.
    monkeypatch.setattr(ollama, "lan_exposure_warning", lambda **k: None)
    monkeypatch.setattr(ollama, "installed_tags", lambda **k: set())

    def _boom(*a: object, **k: object) -> None:
        raise tools.ToolInstallError("kaboom")

    monkeypatch.setattr(tools, "install_tool", _boom)
    cfg = _config()
    cfg.tools = [Tool(name="ados", source="https://example/ados", install_to="sandbox")]
    r = RecordingRunner()
    with pytest.raises(install.InstallError, match="ados"):
        install.run_install(cfg, tmp_path, r)
    assert not any("docker sandbox create" in c for c in r.joined())  # aborted pre-sandbox


def test_install_missing_target_fails_loud() -> None:
    r = RecordingRunner()
    with pytest.raises(install.InstallError):
        install.run_install(_config(), Path("/no/such/dir"), r)
