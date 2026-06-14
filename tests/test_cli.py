from __future__ import annotations

from importlib.metadata import version as pkg_version
from pathlib import Path

import typer.main
from typer.testing import CliRunner

from book_em_danno.cli import app

runner = CliRunner()
EXAMPLE = Path(__file__).resolve().parents[1] / "danno.toml.example"


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    # Derive from package metadata so a version bump doesn't break this test.
    assert result.stdout.strip() == f"danno {pkg_version('book-em-danno')}"


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
    # nothing is executed (no Docker/Ollama present, yet it exits clean).
    result = runner.invoke(app, ["install", "--config", str(EXAMPLE), "--target", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / ".opencode" / "opencode.jsonc").is_file()


def test_install_missing_config_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["install", "--config", str(tmp_path / "nope.toml"), "--target", str(tmp_path)]
    )
    assert result.exit_code == 2
