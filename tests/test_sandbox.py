from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.commands import ollama, sandbox
from book_em_danno.core.exec import CommandFailedError
from conftest import RecordingRunner


def test_default_name() -> None:
    assert sandbox.default_name(Path("/tmp/my-proj")) == "danno-my-proj"


def test_default_name_per_agent_suffix() -> None:
    # Default opencode keeps the bare name; other agents get a suffix so they coexist.
    assert sandbox.default_name(Path("/tmp/my-proj"), "opencode") == "danno-my-proj"
    assert sandbox.default_name(Path("/tmp/my-proj"), "claude") == "danno-my-proj-claude"


def test_create_claude_agent(tmp_path: Path) -> None:
    r = RecordingRunner()
    sandbox.create(r, "danno-x-claude", tmp_path, "claude")
    assert r.joined() == [f"docker sandbox create --name danno-x-claude claude {tmp_path}"]


def test_launch_claude_uses_agent_binary() -> None:
    r = RecordingRunner()
    sandbox.launch(r, "danno-x-claude", agent="claude")
    assert r.joined() == ["docker sandbox exec -it --env-file <env-file> danno-x-claude claude"]


def test_agent_env_opencode_injects_ollama() -> None:
    assert sandbox.agent_env("opencode", "http://h:11434/v1") == [
        "OLLAMA_BASE_URL=http://h:11434/v1"
    ]


def test_agent_env_claude_prefers_oauth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    assert sandbox.agent_env("claude", "u") == ["CLAUDE_CODE_OAUTH_TOKEN=tok"]


def test_agent_env_claude_falls_back_to_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    assert sandbox.agent_env("claude", "u") == ["ANTHROPIC_API_KEY=key"]


def test_agent_env_claude_no_auth_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(CommandFailedError, match="claude setup-token"):
        sandbox.agent_env("claude", "u")


def test_create_command(tmp_path: Path) -> None:
    r = RecordingRunner()
    sandbox.create(r, "probe", tmp_path)
    assert r.joined() == [f"docker sandbox create --name probe opencode {tmp_path}"]


def test_configure_proxy_opens_ollama_hole() -> None:
    r = RecordingRunner()
    sandbox.configure_proxy(r, "probe")
    assert r.joined() == [
        "docker sandbox network proxy probe --policy allow --allow-host localhost:11434"
    ]


def test_provision_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "loopback_warning", lambda **kw: None)
    r = RecordingRunner()
    sandbox.provision(r, "probe", tmp_path)
    assert r.joined() == [
        f"docker sandbox create --name probe opencode {tmp_path}",
        "docker sandbox network proxy probe --policy allow --allow-host localhost:11434",
        "docker sandbox stop probe",
    ]


def test_launch_builds_exec_command() -> None:
    r = RecordingRunner()
    sandbox.launch(r, "probe")
    assert r.joined() == ["docker sandbox exec -it --env-file <env-file> probe opencode"]


def test_shell_command() -> None:
    r = RecordingRunner()
    sandbox.shell(r, "probe")
    assert r.joined() == ["docker sandbox exec -it probe bash"]


def test_rebuild_stops_and_removes_without_force_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `docker sandbox rm` has no -f flag; rebuild must stop-then-rm (no force).
    monkeypatch.setattr(ollama, "loopback_warning", lambda **kw: None)
    r = RecordingRunner()
    sandbox.rebuild(r, "probe", tmp_path)
    assert r.joined() == [
        "docker sandbox stop probe",
        "docker sandbox rm probe",
        f"docker sandbox create --name probe opencode {tmp_path}",
        "docker sandbox network proxy probe --policy allow --allow-host localhost:11434",
        "docker sandbox stop probe",
    ]
    assert all("-f" not in c and "--force" not in c for c in r.commands)


def test_create_is_idempotent_under_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # When executing for real and the sandbox already exists, create is skipped.
    monkeypatch.setattr(sandbox, "sandbox_exists", lambda name: True)
    r = RecordingRunner()
    r.apply, r.dry_run = True, False  # simulate --apply
    sandbox.create(r, "probe", tmp_path)
    assert r.commands == []  # nothing advised/run
