from __future__ import annotations

from importlib.metadata import version as pkg_version
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from book_em_danno.cli import app

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    # Derive from package metadata so a version bump doesn't break this test.
    assert result.stdout.strip() == f"danno {pkg_version('danno')}"


def test_help_shows_three_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("install", "doctor", "sandbox"):
        assert cmd in result.stdout


def test_collapsed_subcommands_are_gone() -> None:
    for removed in ("config", "tools", "ollama"):
        result = runner.invoke(app, [removed])
        assert result.exit_code != 0  # no longer a command


def test_install_accepts_apply_after_subcommand() -> None:
    """Gap A regression guard: `--apply` is a per-command option on `install` (the
    old global placement rejected `install --apply`). Asserted against the parsed
    Click command rather than rendered --help text, which is width/ANSI-dependent."""
    install_cmd = typer.main.get_command(app).commands["install"]  # type: ignore[attr-defined]
    opts = {opt for param in install_cmd.params for opt in param.opts}
    assert "--apply" in opts
    assert "--dry-run" not in opts


def test_dry_run_flag_is_gone() -> None:
    """The collapsed two-mode model dropped --dry-run; it must no longer parse."""
    result = runner.invoke(app, ["install", "--dry-run", "--target", "."])
    assert result.exit_code != 0


def test_install_default_writes_config_without_executing(tmp_path: Path) -> None:
    # Default (advise) mode: the owned config file is written on first run, and
    # nothing is executed (no Docker/Ollama present, yet it exits clean). Use a
    # tool-less config so the test doesn't depend on an ADOS checkout being present
    # (install now fails loud when a configured tool can't be installed).
    cfg = tmp_path / "danno.toml"
    cfg.write_text(
        "[defaults]\n"
        'default_agent = "pm"\n'
        "[backends.ollama]\n"
        'kind = "ollama"\n'
        'base_url = "http://host.docker.internal:11434/v1"\n'
        "[models.gemma]\n"
        'backend = "ollama"\n'
        'tag = "gemma3:27b"\n'
        "[agents]\n"
        'pm = "gemma"\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["install", "--config", str(cfg), "--target", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / ".opencode" / "opencode.jsonc").is_file()


def test_sandbox_start_forwards_args_after_double_dash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `danno sandbox start … -- --resume <id>` forwards the trailing args to the agent.
    import book_em_danno.cli as cli

    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.sandbox_cmd, "start", lambda *a, **k: captured.update(k))
    monkeypatch.setattr(cli, "_resolve_home", lambda *a, **k: None)
    argv = ["sandbox", "start", "--agent", "claude", "--target", str(tmp_path)]
    argv += ["--", "--resume", "id1"]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
    assert captured["agent_args"] == ["--resume", "id1"]


def test_sandbox_shell_passes_apply_env_and_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `sandbox shell` mirrors `start`'s wiring: it resolves the agent home and
    # forwards --apply / --env through to sandbox_cmd.shell.
    import book_em_danno.cli as cli

    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.sandbox_cmd, "shell", lambda *a, **k: captured.update(k))
    monkeypatch.setattr(cli, "_resolve_home", lambda *a, **k: tmp_path / "home")
    argv = ["sandbox", "shell", "--apply", "--target", str(tmp_path), "--env", "K=V"]
    result = runner.invoke(app, argv)
    assert result.exit_code == 0
    assert captured["home"] == tmp_path / "home"
    assert captured["env_pairs"] == ["K=V"]


def test_install_missing_config_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["install", "--config", str(tmp_path / "nope.toml"), "--target", str(tmp_path)]
    )
    assert result.exit_code == 2
