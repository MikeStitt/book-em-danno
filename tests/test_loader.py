from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.config.loader import DannoConfigError, load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "danno.toml.example"


def test_load_example_ok() -> None:
    cfg = load_config(EXAMPLE)
    assert cfg.defaults.default_agent == "pm"
    assert cfg.defaults.profile == "hybrid"
    assert set(cfg.backends) == {"ollama", "cloud", "llamacpp"}
    assert cfg.models["gemma"].tag == "gemma3:27b"
    assert cfg.models["sonnet"].id == "anthropic/claude-sonnet-4-6"
    assert cfg.agents["architect"] == "sonnet"
    assert [t.name for t in cfg.tools] == ["ados"]
    assert cfg.tools[0].install_to == "sandbox"
    assert [p.package for p in cfg.npm] == ["opencode-planner", "@plannotator/opencode@latest"]
    assert cfg.npm[1].config == {"workflow": "plan-agent", "planningAgents": ["plan"]}


def test_missing_file_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(DannoConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_unknown_key_fails_loud(tmp_path: Path) -> None:
    bad = tmp_path / "danno.toml"
    bad.write_text("[defaults]\ndefault_agent = 'pm'\nbogus_key = true\n", encoding="utf-8")
    with pytest.raises(DannoConfigError, match="invalid danno.toml"):
        load_config(bad)


def test_dangling_agent_reference_fails_loud(tmp_path: Path) -> None:
    bad = tmp_path / "danno.toml"
    bad.write_text(
        "[backends.cloud]\nkind = 'cloud'\nprovider = 'anthropic'\n"
        "[models.sonnet]\nbackend = 'cloud'\nid = 'anthropic/claude-sonnet-4-6'\n"
        "[agents]\npm = 'does-not-exist'\n",
        encoding="utf-8",
    )
    with pytest.raises(DannoConfigError, match="unknown model"):
        load_config(bad)


def test_malformed_toml_fails_loud(tmp_path: Path) -> None:
    bad = tmp_path / "danno.toml"
    bad.write_text("[defaults\n", encoding="utf-8")
    with pytest.raises(DannoConfigError, match="invalid TOML"):
        load_config(bad)


def test_sandbox_defaults_to_per_project() -> None:
    assert load_config(EXAMPLE).sandbox.agent_home == "per-project"


def test_bad_agent_home_fails_loud(tmp_path: Path) -> None:
    bad = tmp_path / "danno.toml"
    bad.write_text("[sandbox]\nagent_home = 'bogus'\n", encoding="utf-8")
    with pytest.raises(DannoConfigError, match="invalid agent_home"):
        load_config(bad)


def test_sandbox_unknown_key_fails_loud(tmp_path: Path) -> None:
    bad = tmp_path / "danno.toml"
    bad.write_text("[sandbox]\nagent_home = 'shared'\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(DannoConfigError, match="invalid danno.toml"):
        load_config(bad)


def test_npm_plugins_load(tmp_path: Path) -> None:
    cfg_path = tmp_path / "danno.toml"
    cfg_path.write_text(
        "[[npm]]\npackage = 'opencode-planner'\n\n"
        "[[npm]]\npackage = '@plannotator/opencode@latest'\n"
        "setup = ['curl -fsSL https://plannotator.ai/install.sh | bash']\n"
        "[npm.config]\nworkflow = 'plan-agent'\n",
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert [p.package for p in cfg.npm] == ["opencode-planner", "@plannotator/opencode@latest"]
    assert cfg.npm[0].config is None
    assert cfg.npm[1].config == {"workflow": "plan-agent"}
    assert cfg.npm[1].setup == ["curl -fsSL https://plannotator.ai/install.sh | bash"]


def test_empty_npm_package_fails_loud(tmp_path: Path) -> None:
    bad = tmp_path / "danno.toml"
    bad.write_text("[[npm]]\npackage = ''\n", encoding="utf-8")
    with pytest.raises(DannoConfigError, match="non-empty"):
        load_config(bad)
