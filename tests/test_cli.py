from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from book_em_danno.cli import app

runner = CliRunner()
EXAMPLE = Path(__file__).resolve().parents[1] / "danno.toml.example"


def test_version_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "danno 0.1.0"


def test_help_shows_three_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("install", "doctor", "sandbox"):
        assert cmd in result.stdout


def test_collapsed_subcommands_are_gone() -> None:
    for removed in ("config", "tools", "ollama"):
        result = runner.invoke(app, [removed])
        assert result.exit_code != 0  # no longer a command


def test_install_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--config", str(EXAMPLE), "--dry-run", "install", "--target", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert not (tmp_path / ".opencode" / "opencode.jsonc").exists()


def test_install_missing_config_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["--config", str(tmp_path / "nope.toml"), "install", "--target", str(tmp_path)]
    )
    assert result.exit_code == 2
