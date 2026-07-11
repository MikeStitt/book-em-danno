"""CLI tests for `danno validate` — dry-run and early-exit paths (no Docker)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from book_em_danno.cli import app

runner = CliRunner()

_DANNO_TOML = """\
[backends.ollama]
kind = "ollama"
base_url = "http://host.docker.internal:11434/v1"

[models.gptoss]
backend = "ollama"
tag = "gpt-oss:20b"
context_budget = 32000
output_limit = 8192

[models.gemma]
backend = "ollama"
tag = "gemma3:27b"
context_budget = 32000
output_limit = 8192
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "danno.toml").write_text(_DANNO_TOML)
    return tmp_path


def _invoke(project: Path, *args: str) -> object:
    return runner.invoke(app, ["validate", "--target", str(project), *args])


def test_dry_run_prints_plan_and_exits_zero(project: Path) -> None:
    result = _invoke(project, "--dry-run")
    assert result.exit_code == 0
    assert "danno validate — plan" in result.stdout
    assert "gptoss" in result.stdout and "gemma" in result.stdout


def test_dry_run_only_subset_shown(project: Path) -> None:
    result = _invoke(project, "--dry-run", "--only", "gptoss")
    assert result.exit_code == 0
    assert "sweeping" in result.stdout


def test_unknown_only_fails_loud(project: Path) -> None:
    result = _invoke(project, "--dry-run", "--only", "nope")
    assert result.exit_code == 3
    assert "nope" in result.stdout


def test_html_is_rejected_up_front(project: Path) -> None:
    result = _invoke(project, "--dry-run", "--html")
    assert result.exit_code == 3
    assert "--html is not yet wired" in result.stdout


def test_missing_config_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", "--target", str(tmp_path), "--dry-run"])
    assert result.exit_code == 2  # no danno.toml


def test_baseline_without_token_fails_before_running(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = _invoke(project, "--dry-run", "--baseline")
    assert result.exit_code == 4
    assert "setup-token" in result.stdout or "ANTHROPIC_API_KEY" in result.stdout
