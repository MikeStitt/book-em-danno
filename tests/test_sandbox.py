from __future__ import annotations

import json
from pathlib import Path

import pytest

from book_em_danno.commands import ollama, sandbox
from book_em_danno.config.loader import DannoConfigError
from book_em_danno.core import registry
from book_em_danno.core.exec import CommandFailedError
from conftest import RecordingRunner


def test_default_name() -> None:
    assert sandbox.default_name(Path("/tmp/my-proj")) == "danno-tmp-my-proj"


def test_default_name_per_agent_suffix() -> None:
    # Default opencode keeps the bare name; other agents get a suffix so they coexist.
    assert sandbox.default_name(Path("/tmp/my-proj"), "opencode") == "danno-tmp-my-proj"
    assert sandbox.default_name(Path("/tmp/my-proj"), "claude") == "danno-tmp-my-proj-claude"


def test_default_name_distinguishes_same_basename() -> None:
    # Parent prefix keeps ~/work/acme and ~/clients/acme (and worktree dirs) apart.
    assert sandbox.default_name(Path("/work/acme")) == "danno-work-acme"
    assert sandbox.default_name(Path("/clients/acme")) == "danno-clients-acme"
    assert sandbox.default_name(Path("/work/acme/main")) == "danno-acme-main"


def test_create_claude_agent(tmp_path: Path) -> None:
    r = RecordingRunner()
    sandbox.create(r, "danno-x-claude", tmp_path, "claude")
    assert r.joined() == [f"docker sandbox create --name danno-x-claude claude {tmp_path}"]


def _assert_launch_cmd(cmd: list[str], name: str, agent: str, repo: str = "/repo") -> None:
    """Assert a launch exec command, tolerating the generated env-file temp path."""
    assert cmd[:7] == ["docker", "sandbox", "exec", "-it", "-w", repo, "--env-file"]
    assert cmd[7]  # a real env-file path (created then unlinked); value is dynamic
    assert cmd[8:] == [name, agent]


def test_launch_claude_uses_agent_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    r = RecordingRunner()
    sandbox.launch(r, "danno-x-claude", Path("/repo"), agent="claude")
    assert len(r.commands) == 1
    _assert_launch_cmd(r.commands[0], "danno-x-claude", "claude")


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
    sandbox.launch(r, "probe", Path("/repo"))
    assert len(r.commands) == 1
    _assert_launch_cmd(r.commands[0], "probe", "opencode")


def test_shell_command() -> None:
    r = RecordingRunner()
    sandbox.shell(r, "probe")
    assert r.joined() == ["docker sandbox exec -it probe bash"]


def test_start_fails_loud_when_not_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Default (no --apply) launch of an unprovisioned sandbox must fail loud with
    # the fix, not let `docker sandbox exec` error on a missing sandbox.
    monkeypatch.setattr(sandbox, "sandbox_exists", lambda name: False)
    with pytest.raises(CommandFailedError, match="not provisioned"):
        sandbox.start(RecordingRunner(), "probe", tmp_path)


def test_start_launches_existing_without_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Existing sandbox, no --apply: skip provisioning, just launch.
    monkeypatch.setattr(sandbox, "sandbox_exists", lambda name: True)
    r = RecordingRunner()
    sandbox.start(r, "probe", tmp_path)
    assert len(r.commands) == 1  # only the launch, no create/proxy/stop
    _assert_launch_cmd(r.commands[0], "probe", "opencode", repo=str(tmp_path))


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
    r.apply = True  # simulate --apply
    sandbox.create(r, "probe", tmp_path)
    assert r.commands == []  # nothing advised/run


# --- agent-home: env relocation -------------------------------------------------


def test_agent_env_claude_relocates_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    assert sandbox.agent_env("claude", "u", Path("/h")) == [
        "CLAUDE_CODE_OAUTH_TOKEN=tok",
        "CLAUDE_CONFIG_DIR=/h",
    ]


def test_agent_env_opencode_relocates_config_not_data() -> None:
    # Config goes on the mounted home; the sqlite data dir is left VM-local so WAL
    # works (the mounted home is virtiofs, which can't do WAL). See agent_env docs.
    assert sandbox.agent_env("opencode", "http://h:11434/v1", Path("/h")) == [
        "OLLAMA_BASE_URL=http://h:11434/v1",
        "XDG_CONFIG_HOME=/h/config",
    ]


# --- agent-home: create mounts a second workspace -------------------------------


def test_create_mounts_agent_home(tmp_path: Path) -> None:
    r = RecordingRunner()
    home = tmp_path / "home"
    sandbox.create(r, "probe", tmp_path, "claude", home=home)
    assert r.joined() == [
        f"mkdir -p {home}",
        f"docker sandbox create --name probe claude {tmp_path} {home}",
    ]


def test_create_no_home_is_single_mount(tmp_path: Path) -> None:
    r = RecordingRunner()
    sandbox.create(r, "probe", tmp_path)
    assert r.joined() == [f"docker sandbox create --name probe opencode {tmp_path}"]


# --- agent-home: registry guard -------------------------------------------------


def test_create_advises_record_by_default(tmp_path: Path) -> None:
    reg = tmp_path / "sandboxes.json"
    r = RecordingRunner()
    sandbox.create(r, "probe", tmp_path, registry_path=reg)
    assert not reg.exists()  # nothing recorded outside --apply


def test_create_records_under_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "sandbox_exists", lambda name: False)
    reg = tmp_path / "sandboxes.json"
    r = RecordingRunner()
    r.apply = True
    sandbox.create(r, "probe", tmp_path / "proj", registry_path=reg)
    assert registry.lookup(reg, "probe") == {"target": str(tmp_path / "proj"), "agent": "opencode"}


def test_create_warns_on_name_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = tmp_path / "sandboxes.json"
    registry.record(reg, "probe", "/somewhere/else", "opencode")
    warnings: list[str] = []
    monkeypatch.setattr(sandbox, "log_warn", lambda m: warnings.append(m))
    sandbox.create(RecordingRunner(), "probe", tmp_path, registry_path=reg)
    assert any("already maps to /somewhere/else" in w for w in warnings)


# --- agent-home: key resolution -------------------------------------------------


def test_resolve_agent_home_forms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "agent-home"
    monkeypatch.setattr(sandbox, "_agent_home_root", lambda: root)
    target = tmp_path / "proj"

    assert sandbox.resolve_agent_home("ephemeral", target, "danno-x") is None
    assert sandbox.resolve_agent_home("per-project", target, "danno-x") == root / "danno-x"
    assert sandbox.resolve_agent_home("shared", target, "danno-x") == root / "shared"
    assert sandbox.resolve_agent_home("group:acme", target, "danno-x") == root / "groups" / "acme"
    # Explicit absolute path is taken as-is.
    assert sandbox.resolve_agent_home("/opt/home", target, "danno-x") == Path("/opt/home")


def test_resolve_agent_home_per_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "agent-home"
    monkeypatch.setattr(sandbox, "_agent_home_root", lambda: root)
    monkeypatch.setattr(sandbox, "_git_common_dir", lambda t: Path("/work/acme/.git"))
    home = sandbox.resolve_agent_home("per-repo", tmp_path, "danno-x")
    assert home is not None
    assert home.parent == root / "repos"
    assert home.name.startswith("acme-")  # readable prefix + hash


def test_resolve_agent_home_relative_against_workspace_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "_agent_home_root", lambda: tmp_path / "agent-home")
    target = tmp_path / "ws" / "login"
    ws = tmp_path / "ws"
    home = sandbox.resolve_agent_home(".shared/home", target, "danno-x", relative_base=ws)
    assert home == (ws / ".shared" / "home").resolve()


# --- agent-home: resolve_home (config + workspace inheritance + hints) -----------


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_resolve_home_defaults_to_per_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "agent-home"
    monkeypatch.setattr(sandbox, "_agent_home_root", lambda: root)
    target = tmp_path / "proj"
    target.mkdir()
    assert sandbox.resolve_home(target, "danno-x") == root / "danno-x"


def test_resolve_home_reads_own_sandbox_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "agent-home"
    monkeypatch.setattr(sandbox, "_agent_home_root", lambda: root)
    target = tmp_path / "proj"
    _write(target / "danno.toml", '[sandbox]\nagent_home = "shared"\n')
    assert sandbox.resolve_home(target, "danno-x") == root / "shared"


def test_resolve_home_inherits_workspace_with_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "_agent_home_root", lambda: tmp_path / "agent-home")
    ws = tmp_path / "acme"
    _write(ws / "danno.workspace.toml", '[sandbox]\nagent_home = ".shared/home"\n')
    target = ws / "login"
    target.mkdir()
    # Relative path resolves against the WORKSPACE dir, not the target.
    assert sandbox.resolve_home(target, "danno-x") == (ws / ".shared" / "home").resolve()


def test_resolve_home_misplaced_cwd_hint_fires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "_agent_home_root", lambda: tmp_path / "agent-home")
    _write(tmp_path / "proj" / "danno.toml", "[project]\n")
    sub = tmp_path / "proj" / "sub"
    sub.mkdir()
    warnings: list[str] = []
    monkeypatch.setattr(sandbox, "log_warn", lambda m: warnings.append(m))
    sandbox.resolve_home(sub, "danno-x")
    assert any("project root looks like" in w for w in warnings)


def test_resolve_home_hint_silent_for_subpackage_with_own_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "_agent_home_root", lambda: tmp_path / "agent-home")
    _write(tmp_path / "proj" / "danno.toml", "[project]\n")
    sub = tmp_path / "proj" / "pkg"
    _write(sub / "danno.toml", "[project]\n")  # sub carries its own config
    warnings: list[str] = []
    monkeypatch.setattr(sandbox, "log_warn", lambda m: warnings.append(m))
    sandbox.resolve_home(sub, "danno-x")
    assert not any("project root looks like" in w for w in warnings)


def test_resolve_home_footgun_warns_when_home_inside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "proj"
    _write(target / "danno.toml", '[sandbox]\nagent_home = "./.agent-home"\n')
    warnings: list[str] = []
    monkeypatch.setattr(sandbox, "log_warn", lambda m: warnings.append(m))
    sandbox.resolve_home(target, "danno-x")
    assert any("inside the repo" in w for w in warnings)


def test_resolve_home_malformed_config_fails_loud(tmp_path: Path) -> None:
    target = tmp_path / "proj"
    _write(target / "danno.toml", '[sandbox]\nagent_home = "bogus"\n')
    with pytest.raises(DannoConfigError, match="invalid"):
        sandbox.resolve_home(target, "danno-x")


# --- agent-home: claude onboarding seed -----------------------------------------


def test_seed_onboarding_creates_and_merges(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sandbox.seed_onboarding(home)
    data = json.loads((home / ".claude.json").read_text())
    assert data["hasCompletedOnboarding"] is True
    assert "theme" in data


def test_seed_onboarding_does_not_clobber(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"theme": "light", "mcpServers": {"x": 1}}')
    sandbox.seed_onboarding(home)
    data = json.loads((home / ".claude.json").read_text())
    assert data["theme"] == "light"  # existing key preserved
    assert data["mcpServers"] == {"x": 1}  # unrelated key preserved
    assert data["hasCompletedOnboarding"] is True  # added


# --- agent-home: ls -------------------------------------------------------------


def test_ls_prints_registered_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = tmp_path / "sandboxes.json"
    registry.record(reg, "danno-work-acme", "/work/acme", "claude")
    monkeypatch.setattr(sandbox, "live_sandbox_names", lambda: {"danno-work-acme"})
    lines: list[str] = []
    monkeypatch.setattr(sandbox, "log_info", lambda m: lines.append(m))
    sandbox.ls(reg)
    assert any("danno-work-acme → /work/acme (claude) [live]" in line for line in lines)
