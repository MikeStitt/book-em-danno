from __future__ import annotations

import json
from pathlib import Path

import pytest

from book_em_danno.commands import ollama, sandbox
from book_em_danno.config.loader import DannoConfigError
from book_em_danno.config.schema import (
    DannoConfig,
    Model,
    OllamaBackend,
    OpenAIBackend,
)
from book_em_danno.core import registry
from book_em_danno.core.exec import CommandFailedError, Runner
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


def test_provision_existing_sandbox_starts_before_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Re-provision under --apply when the sandbox already exists (stopped): create is
    # skipped, and the VM is started before `network proxy` (which 400s on a stopped
    # VM). Order must be ensure-running → proxy → stop.
    monkeypatch.setattr(ollama, "loopback_warning", lambda **kw: None)
    monkeypatch.setattr(sandbox, "sandbox_exists", lambda name: True)
    r = RecordingRunner()
    r.apply = True
    sandbox.provision(r, "probe", tmp_path)
    assert r.joined() == [
        # create skipped (already exists) — no `docker sandbox create`
        "docker sandbox exec probe true",  # ensure running before proxy
        "docker sandbox network proxy probe --policy allow --allow-host localhost:11434",
        "docker sandbox stop probe",
    ]


def test_launch_builds_exec_command() -> None:
    r = RecordingRunner()
    sandbox.launch(r, "probe", Path("/repo"))
    assert len(r.commands) == 1
    _assert_launch_cmd(r.commands[0], "probe", "opencode")


def test_launch_forwards_agent_args() -> None:
    # `--` passthrough: extra args land verbatim after the agent name.
    r = RecordingRunner()
    sandbox.launch(r, "probe", Path("/repo"), agent="opencode", agent_args=["--resume", "abc123"])
    assert r.commands[0][8:] == ["probe", "opencode", "--resume", "abc123"]


# --- claurst (interactive, local-Ollama clone hosted in the `shell` image) ---


def test_default_name_claurst_suffix() -> None:
    assert sandbox.default_name(Path("/tmp/my-proj"), "claurst") == "danno-tmp-my-proj-claurst"


def test_create_claurst_uses_shell_image(tmp_path: Path) -> None:
    # claurst has no prebuilt image: the create command rides `shell`, but the sandbox
    # name keeps the claurst label.
    r = RecordingRunner()
    sandbox.create(r, "danno-x-claurst", tmp_path, "claurst")
    assert r.joined() == [f"docker sandbox create --name danno-x-claurst shell {tmp_path}"]


def test_provision_claurst_installs_after_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same create(shell)/proxy/stop order as opencode, plus a trailing install exec —
    # placed after `stop` so it auto-starts the VM with the egress allow-policy armed.
    monkeypatch.setattr(ollama, "loopback_warning", lambda **kw: None)
    r = RecordingRunner()
    sandbox.provision(r, "probe", tmp_path, agent="claurst")
    joined = r.joined()
    assert joined[:3] == [
        f"docker sandbox create --name probe shell {tmp_path}",
        "docker sandbox network proxy probe --policy allow --allow-host localhost:11434",
        "docker sandbox stop probe",
    ]
    assert joined[3].startswith("docker sandbox exec probe bash -lc ")
    assert "install -m 0755" in joined[3] and "claurst --version" in joined[3]


def test_provision_opencode_does_not_install_claurst(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ollama, "loopback_warning", lambda **kw: None)
    r = RecordingRunner()
    sandbox.provision(r, "probe", tmp_path)  # default opencode
    assert not any("claurst" in c for c in r.joined())


def test_launch_claurst_wraps_relay_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    r = RecordingRunner()
    sandbox.launch(r, "probe", Path("/repo"), agent="claurst", model="ollama/gemma4:26b")
    cmd = r.commands[0]
    assert cmd[8:11] == ["probe", "bash", "-lc"]
    script = cmd[11]
    # the interactive claurst command: model passed as -m, NO -p (it must open the TUI)
    assert "OLLAMA_HOST=http://127.0.0.1:11434 claurst -m ollama/gemma4:26b" in script
    assert "claurst -p" not in script
    # the relay bracket is reused from the headless path (backgrounded + reaped on exit)
    assert "RELAY_PY" in script and "trap 'kill $DANNO_RELAY_PID" in script


def test_launch_claurst_forwards_passthru(monkeypatch: pytest.MonkeyPatch) -> None:
    r = RecordingRunner()
    sandbox.launch(
        r,
        "probe",
        Path("/repo"),
        agent="claurst",
        model="ollama/g:1b",
        agent_args=["--resume", "x"],
    )
    script = r.commands[0][11]
    assert "claurst -m ollama/g:1b --resume x" in script


def test_launch_claurst_cloud_runs_direct_no_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    # Layer 2: a cloud ref runs claurst directly (it dials the provider via HTTPS_PROXY),
    # so the command is a plain claurst argv — no bash -lc relay bracket, no OLLAMA_HOST.
    r = RecordingRunner()
    sandbox.launch(r, "probe", Path("/repo"), agent="claurst", model="nvidia/nvidia/nemotron")
    cmd = r.commands[0]
    assert cmd[-3:] == ["claurst", "-m", "nvidia/nvidia/nemotron"]
    assert "bash" not in cmd[8:]  # no relay wrapper
    assert not any("OLLAMA_HOST" in c or "RELAY_PY" in c for c in cmd)


def test_agent_env_claurst_relocates_home() -> None:
    assert sandbox.agent_env("claurst", "u") == []
    # With a home, HOME is relocated AND claurst is pointed at the danno-generated
    # registry overlay under {home}/.claurst (Bug 4/7 fix for the local Ollama path).
    assert sandbox.agent_env("claurst", "u", home=Path("/h")) == [
        "HOME=/h",
        "CLAURST_MODELS_PATH=/h/.claurst/models.json",
    ]


def _claurst_cfg() -> DannoConfig:
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://h:11434/v1"),
            "nvidia": OpenAIBackend(
                kind="openai",
                base_url="https://integrate.api.nvidia.com/v1",
                api_key_env="NVIDIA_API_KEY",
            ),
            # An openai backend at a host claurst has no provider mapping for.
            "other": OpenAIBackend(
                kind="openai", base_url="https://api.example.com/v1", api_key_env="EXAMPLE_KEY"
            ),
        },
        models={
            "gemma4": Model(backend="ollama", tag="gemma4:26b"),
            "nemotron": Model(backend="nvidia", tag="nvidia/nemotron"),
            "exotic": Model(backend="other", tag="exo-1"),
        },
    )


def test_resolve_claurst_model_local_name() -> None:
    assert sandbox.resolve_claurst_model(_claurst_cfg(), "gemma4") == "ollama/gemma4:26b"


def test_resolve_claurst_model_raw_ollama_ref_passthrough() -> None:
    assert sandbox.resolve_claurst_model(_claurst_cfg(), "ollama/foo:1b") == "ollama/foo:1b"


def test_resolve_claurst_model_nim_cloud_resolves() -> None:
    # Layer 2: an NVIDIA NIM model is now reachable (fork build honors the egress proxy),
    # resolved into claurst's own provider namespace.
    assert sandbox.resolve_claurst_model(_claurst_cfg(), "nemotron") == "nvidia/nvidia/nemotron"


def test_resolve_claurst_model_unmapped_cloud_host_fails_loud() -> None:
    # A cloud backend at a host claurst has no provider mapping for must fail loud, not launch.
    with pytest.raises(CommandFailedError, match="can't reach model 'exotic'"):
        sandbox.resolve_claurst_model(_claurst_cfg(), "exotic")


def test_resolve_claurst_model_raw_cloud_ref_fails_loud() -> None:
    with pytest.raises(CommandFailedError, match="anthropic"):
        sandbox.resolve_claurst_model(_claurst_cfg(), "anthropic/claude-sonnet-4-6")


def test_resolve_claurst_model_unknown_name_fails_loud() -> None:
    with pytest.raises(CommandFailedError, match="not defined"):
        sandbox.resolve_claurst_model(_claurst_cfg(), "nope")


def test_claurst_cloud_key_env_maps_provider_to_var() -> None:
    cfg = _claurst_cfg()
    assert sandbox.claurst_cloud_key_env(cfg, "nemotron") == "NVIDIA_API_KEY"
    assert sandbox.claurst_cloud_key_env(cfg, "gemma4") is None  # local Ollama needs no key
    assert sandbox.claurst_cloud_key_env(cfg, "ollama/foo:1b") is None  # raw ollama ref


def test_claurst_cloud_env_lines_injects_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-secret")
    lines = sandbox.claurst_cloud_env_lines(_claurst_cfg(), "nemotron")
    assert lines == ["NVIDIA_API_KEY=nvapi-secret"]
    assert sandbox.claurst_cloud_env_lines(_claurst_cfg(), "gemma4") == []  # local: no injection


def test_claurst_cloud_env_lines_missing_key_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(CommandFailedError, match="NVIDIA_API_KEY"):
        sandbox.claurst_cloud_env_lines(_claurst_cfg(), "nemotron")


def test_resolve_model_for_agent_rejects_non_claurst(tmp_path: Path) -> None:
    # -m on claude/opencode must fail loud rather than be silently ignored. The agent
    # check precedes any config load, so no danno.toml is needed here.
    with pytest.raises(CommandFailedError, match="only supported with"):
        sandbox.resolve_model_for_agent(tmp_path, "opencode", "gemma4")


def test_resolve_model_for_agent_claurst_loads_and_resolves(tmp_path: Path) -> None:
    (tmp_path / "danno.toml").write_text(
        '[backends.ollama]\nkind = "ollama"\nbase_url = "http://h:11434/v1"\n'
        '[models.gemma4]\nbackend = "ollama"\ntag = "gemma4:26b"\n',
        encoding="utf-8",
    )
    assert sandbox.resolve_model_for_agent(tmp_path, "claurst", "gemma4") == "ollama/gemma4:26b"


def test_resolve_claurst_start_local_returns_ref_no_env(tmp_path: Path) -> None:
    (tmp_path / "danno.toml").write_text(
        '[backends.ollama]\nkind = "ollama"\nbase_url = "http://h:11434/v1"\n'
        '[models.gemma4]\nbackend = "ollama"\ntag = "gemma4:26b"\n',
        encoding="utf-8",
    )
    ref, env_lines = sandbox.resolve_claurst_start(tmp_path, "claurst", "gemma4")
    assert ref == "ollama/gemma4:26b"
    assert env_lines == []  # local Ollama injects no key


def test_resolve_claurst_start_cloud_returns_ref_and_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-secret")
    (tmp_path / "danno.toml").write_text(
        '[backends.nvidia]\nkind = "openai"\n'
        'base_url = "https://integrate.api.nvidia.com/v1"\napi_key_env = "NVIDIA_API_KEY"\n'
        '[models.nemotron]\nbackend = "nvidia"\ntag = "nvidia/nemotron"\n',
        encoding="utf-8",
    )
    ref, env_lines = sandbox.resolve_claurst_start(tmp_path, "claurst", "nemotron")
    assert ref == "nvidia/nvidia/nemotron"
    assert env_lines == ["NVIDIA_API_KEY=nvapi-secret"]


def test_emit_claurst_config_writes_overlay_and_settings(tmp_path: Path) -> None:
    (tmp_path / "danno.toml").write_text(
        '[backends.ollama]\nkind = "ollama"\nbase_url = "http://h:11434/v1"\n'
        '[models.gemma4]\nbackend = "ollama"\ntag = "gemma4:26b"\n'
        '[agents]\nbuild = "gemma4"\n',
        encoding="utf-8",
    )
    home = tmp_path / "home"
    sandbox._emit_claurst_config(Runner(apply=True), tmp_path, home)
    overlay = json.loads((home / ".claurst" / "models.json").read_text())
    assert overlay["ollama"]["models"]["gemma4:26b"]["tool_call"] is True
    settings = json.loads((home / ".claurst" / "settings.json").read_text())
    assert settings["agents"]["build"] == {"model": "ollama/gemma4:26b"}


# --- occ (open-claude-code, a Node/ESM clone git-cloned + patched in the `shell` image) ---


def _occ_cfg() -> DannoConfig:
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://h:11434/v1"),
            "nvidia": OpenAIBackend(
                kind="openai",
                base_url="https://integrate.api.nvidia.com/v1",
                api_key_env="NVIDIA_API_KEY",
            ),
        },
        models={
            "gemma4": Model(backend="ollama", tag="gemma4:26b"),
            "nemotron": Model(backend="nvidia", tag="nvidia/nemotron"),
        },
    )


def test_default_name_occ_suffix() -> None:
    assert sandbox.default_name(Path("/tmp/my-proj"), "occ") == "danno-tmp-my-proj-occ"


def test_docker_image_occ_uses_shell() -> None:
    assert sandbox._docker_image("occ") == "shell"


def test_create_occ_uses_shell_image(tmp_path: Path) -> None:
    r = RecordingRunner()
    sandbox.create(r, "danno-x-occ", tmp_path, "occ")
    assert r.joined() == [f"docker sandbox create --name danno-x-occ shell {tmp_path}"]


def test_provision_occ_installs_after_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same create(shell)/proxy/stop order as claurst, plus a trailing clone/patch exec.
    monkeypatch.setattr(ollama, "loopback_warning", lambda **kw: None)
    r = RecordingRunner()
    sandbox.provision(r, "probe", tmp_path, agent="occ")
    joined = r.joined()
    assert joined[:3] == [
        f"docker sandbox create --name probe shell {tmp_path}",
        "docker sandbox network proxy probe --policy allow --allow-host localhost:11434",
        "docker sandbox stop probe",
    ]
    assert joined[3].startswith("docker sandbox exec probe bash -lc ")
    assert "git clone" in joined[3] and "checkout" in joined[3]


def test_provision_opencode_does_not_install_occ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ollama, "loopback_warning", lambda **kw: None)
    r = RecordingRunner()
    sandbox.provision(r, "probe", tmp_path)  # default opencode
    assert not any("git clone" in c for c in r.joined())


def test_agent_env_occ_relocates_home_only() -> None:
    # occ's OpenAI env is set inline in the launch command, so agent_env only relocates HOME.
    assert sandbox.agent_env("occ", "u") == []
    assert sandbox.agent_env("occ", "u", home=Path("/h")) == ["HOME=/h"]


def test_launch_occ_wraps_relay_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    r = RecordingRunner()
    sandbox.launch(r, "probe", Path("/repo"), agent="occ", model="ollama/gemma4:26b")
    cmd = r.commands[0]
    assert cmd[8:11] == ["probe", "bash", "-lc"]
    script = cmd[11]
    # bare ollama tag on -m; NO -p (interactive TUI); relay bracket reused
    assert "-m gemma4:26b" in script
    assert "ollama/gemma4:26b" not in script
    assert "OPENAI_BASE_URL=http://127.0.0.1:11434/v1" in script
    assert "CLAUDE_CODE_STREAMING=0" in script
    assert "RELAY_PY" in script and "trap 'kill $DANNO_RELAY_PID" in script


def test_launch_occ_cloud_uses_shim_no_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    r = RecordingRunner()
    sandbox.launch(r, "probe", Path("/repo"), agent="occ", model="nvidia/qwen/q3")
    script = r.commands[0][11]
    assert "NODE_OPTIONS=--import=" in script
    assert "-m qwen/q3" in script
    assert "RELAY_PY" not in script  # no relay on the cloud path


def test_resolve_occ_model_local_name() -> None:
    assert sandbox.resolve_occ_model(_occ_cfg(), "gemma4") == "ollama/gemma4:26b"


def test_resolve_occ_model_raw_ollama_ref_passthrough() -> None:
    assert sandbox.resolve_occ_model(_occ_cfg(), "ollama/foo:1b") == "ollama/foo:1b"


def test_resolve_occ_model_cloud_resolves() -> None:
    assert sandbox.resolve_occ_model(_occ_cfg(), "nemotron") == "nvidia/nvidia/nemotron"


def test_resolve_occ_model_raw_cloud_ref_fails_loud() -> None:
    with pytest.raises(CommandFailedError, match="can't be wired"):
        sandbox.resolve_occ_model(_occ_cfg(), "openai/gpt-4o")


def test_resolve_occ_model_unknown_name_fails_loud() -> None:
    with pytest.raises(CommandFailedError, match="not defined"):
        sandbox.resolve_occ_model(_occ_cfg(), "nope")


def test_occ_cloud_env_lines_injects_base_url_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-secret")
    lines = sandbox.occ_cloud_env_lines(_occ_cfg(), "nemotron")
    assert lines == [
        "OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1",
        "OPENAI_API_KEY=nvapi-secret",
    ]
    assert sandbox.occ_cloud_env_lines(_occ_cfg(), "gemma4") == []  # local: no injection


def test_occ_cloud_env_lines_missing_key_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(CommandFailedError, match="NVIDIA_API_KEY"):
        sandbox.occ_cloud_env_lines(_occ_cfg(), "nemotron")


def test_resolve_start_dispatches_occ_local(tmp_path: Path) -> None:
    (tmp_path / "danno.toml").write_text(
        '[backends.ollama]\nkind = "ollama"\nbase_url = "http://h:11434/v1"\n'
        '[models.gemma4]\nbackend = "ollama"\ntag = "gemma4:26b"\n',
        encoding="utf-8",
    )
    ref, env_lines = sandbox.resolve_start(tmp_path, "occ", "gemma4")
    assert ref == "ollama/gemma4:26b"
    assert env_lines == []


def test_resolve_start_dispatches_occ_cloud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-secret")
    (tmp_path / "danno.toml").write_text(
        '[backends.nvidia]\nkind = "openai"\n'
        'base_url = "https://integrate.api.nvidia.com/v1"\napi_key_env = "NVIDIA_API_KEY"\n'
        '[models.nemotron]\nbackend = "nvidia"\ntag = "nvidia/nemotron"\n',
        encoding="utf-8",
    )
    ref, env_lines = sandbox.resolve_start(tmp_path, "occ", "nemotron")
    assert ref == "nvidia/nvidia/nemotron"
    assert env_lines == [
        "OPENAI_BASE_URL=https://integrate.api.nvidia.com/v1",
        "OPENAI_API_KEY=nvapi-secret",
    ]


def test_resolve_model_for_agent_accepts_occ(tmp_path: Path) -> None:
    (tmp_path / "danno.toml").write_text(
        '[backends.ollama]\nkind = "ollama"\nbase_url = "http://h:11434/v1"\n'
        '[models.gemma4]\nbackend = "ollama"\ntag = "gemma4:26b"\n',
        encoding="utf-8",
    )
    assert sandbox.resolve_model_for_agent(tmp_path, "occ", "gemma4") == "ollama/gemma4:26b"


def _write_opencode_cfg(target: Path, body: str) -> None:
    (target / ".opencode").mkdir(parents=True, exist_ok=True)
    (target / ".opencode" / "opencode.jsonc").write_text(body, encoding="utf-8")


def test_reconcile_env_refs_fails_loud_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_opencode_cfg(tmp_path, '{ "apiKey": "{env:NVIDIA_API_KEY}" }')
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(CommandFailedError, match="NVIDIA_API_KEY"):
        sandbox.reconcile_env_refs(tmp_path, [], [])


def test_reconcile_env_refs_auto_injects_from_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_opencode_cfg(tmp_path, '{ "apiKey": "{env:NVIDIA_API_KEY}" }')
    monkeypatch.setenv("NVIDIA_API_KEY", "nvapi-secret")
    assert sandbox.reconcile_env_refs(tmp_path, [], []) == ["NVIDIA_API_KEY=nvapi-secret"]


def test_reconcile_env_refs_passes_when_supplied_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_opencode_cfg(tmp_path, '{ "apiKey": "{env:NVIDIA_API_KEY}" }')
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    # Explicitly passed via --env → accepted, not duplicated from host.
    pairs = sandbox.reconcile_env_refs(tmp_path, ["NVIDIA_API_KEY=nvapi-x"], [])
    assert pairs == ["NVIDIA_API_KEY=nvapi-x"]


def test_reconcile_env_refs_empty_value_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The exact footgun: `--env NVIDIA_API_KEY=` (empty) must fail, not slip through.
    _write_opencode_cfg(tmp_path, '{ "apiKey": "{env:NVIDIA_API_KEY}" }')
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(CommandFailedError, match="NVIDIA_API_KEY"):
        sandbox.reconcile_env_refs(tmp_path, ["NVIDIA_API_KEY="], [])


def test_reconcile_env_refs_noop_without_refs(tmp_path: Path) -> None:
    # No opencode.jsonc (or no {env:…}) → returns env_pairs unchanged, never raises.
    assert sandbox.reconcile_env_refs(tmp_path, ["FOO=bar"], []) == ["FOO=bar"]


def test_resolve_env_refs_reports_missing_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The non-raising core the validator uses: missing keys come back in the second
    # tuple slot (to warn on) instead of aborting.
    _write_opencode_cfg(tmp_path, '{ "apiKey": "{env:NVIDIA_API_KEY}" }')
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    augmented, missing = sandbox.resolve_env_refs(tmp_path, [], [])
    assert augmented == []
    assert missing == ["NVIDIA_API_KEY"]


def test_shell_mirrors_start_with_bash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # shell is start minus the agent launch: same -w/env-file session setup, the
    # only difference being the container command (`bash`, not the agent binary).
    monkeypatch.setattr(sandbox, "sandbox_exists", lambda name: True)
    r = RecordingRunner()
    sandbox.shell(r, "probe", tmp_path)
    assert len(r.commands) == 1  # only the exec, no create/proxy/stop
    _assert_launch_cmd(r.commands[0], "probe", "bash", repo=str(tmp_path))


def test_shell_fails_loud_when_not_provisioned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Like start: opening a shell on an unprovisioned sandbox without --apply must
    # fail loud, not let `docker sandbox exec` error on a missing sandbox.
    monkeypatch.setattr(sandbox, "sandbox_exists", lambda name: False)
    with pytest.raises(CommandFailedError, match="not provisioned"):
        sandbox.shell(RecordingRunner(), "probe", tmp_path)


def test_shell_provisions_under_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Under --apply, shell provisions first (create/proxy/stop) then opens the shell,
    # exactly as start provisions then launches.
    monkeypatch.setattr(ollama, "loopback_warning", lambda **kw: None)
    monkeypatch.setattr(sandbox, "sandbox_exists", lambda name: False)
    r = RecordingRunner()
    r.apply = True
    sandbox.shell(r, "probe", tmp_path)
    assert r.joined()[:3] == [
        f"docker sandbox create --name probe opencode {tmp_path}",
        "docker sandbox network proxy probe --policy allow --allow-host localhost:11434",
        "docker sandbox stop probe",
    ]
    _assert_launch_cmd(r.commands[3], "probe", "bash", repo=str(tmp_path))


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
    workspace = Path("/work/proj")
    sandbox.seed_onboarding(home, workspace)
    data = json.loads((home / ".claude.json").read_text())
    assert data["hasCompletedOnboarding"] is True
    assert "theme" in data
    # Per-workspace trust is pre-accepted so the "trust this folder" dialog can't
    # block a fresh launch (keyed by the in-container path == host path).
    proj = data["projects"]["/work/proj"]
    assert proj["hasTrustDialogAccepted"] is True
    assert proj["hasCompletedProjectOnboarding"] is True


def test_seed_onboarding_does_not_clobber(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(
        '{"theme": "light", "mcpServers": {"x": 1}, '
        '"projects": {"/work/proj": {"hasTrustDialogAccepted": false, "keep": 1}}}'
    )
    sandbox.seed_onboarding(home, Path("/work/proj"))
    data = json.loads((home / ".claude.json").read_text())
    assert data["theme"] == "light"  # existing key preserved
    assert data["mcpServers"] == {"x": 1}  # unrelated key preserved
    assert data["hasCompletedOnboarding"] is True  # added
    proj = data["projects"]["/work/proj"]
    assert proj["hasTrustDialogAccepted"] is False  # existing trust value not clobbered
    assert proj["keep"] == 1  # unrelated per-project key preserved


# --- agent-home: ls -------------------------------------------------------------


def test_ls_prints_registered_targets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = tmp_path / "sandboxes.json"
    registry.record(reg, "danno-work-acme", "/work/acme", "claude")
    monkeypatch.setattr(sandbox, "live_sandbox_names", lambda: {"danno-work-acme"})
    lines: list[str] = []
    monkeypatch.setattr(sandbox, "log_info", lambda m: lines.append(m))
    sandbox.ls(reg)
    assert any("danno-work-acme → /work/acme (claude) [live]" in line for line in lines)


# --- Phase 1: [env] reaches the session env-file (integration through _exec_session) ---


class _EnvCapturingRunner(RecordingRunner):
    """Records commands AND snapshots each exec's --env-file content before the
    caller's `finally` unlinks it — so tests can assert the assembled env lines."""

    def __init__(self) -> None:
        super().__init__()
        self.env_snapshots: list[list[str]] = []

    def _snapshot(self, cmd: list[str]) -> None:
        if "--env-file" in cmd:
            path = Path(cmd[cmd.index("--env-file") + 1])
            if path.is_file():
                self.env_snapshots.append(path.read_text(encoding="utf-8").splitlines())

    def advise(self, cmd: list[str], why: str, **kw: object) -> list[str]:  # type: ignore[override]
        self._snapshot(cmd)
        return super().advise(cmd, why, **kw)  # type: ignore[arg-type]

    def run(self, cmd: list[str], why: str, **kw: object) -> list[str]:  # type: ignore[override]
        self._snapshot(cmd)
        return super().run(cmd, why, **kw)  # type: ignore[arg-type]


def test_launch_opencode_env_file_includes_toml_env(tmp_path: Path) -> None:
    # A danno.toml [env] KEY lands in the opencode session env-file, composed on top
    # of the agent default (OLLAMA_BASE_URL).
    (tmp_path / "danno.toml").write_text('[env]\nMY_FLAG = "on"\n', encoding="utf-8")
    r = _EnvCapturingRunner()
    sandbox.launch(r, "probe", tmp_path, agent="opencode")
    assert r.env_snapshots, "no env-file was captured"
    env = dict(line.split("=", 1) for line in r.env_snapshots[0])
    assert env["MY_FLAG"] == "on"
    assert env["OLLAMA_BASE_URL"] == sandbox.DEFAULT_OLLAMA_URL


def test_launch_opencode_cli_env_overrides_toml_env(tmp_path: Path) -> None:
    (tmp_path / "danno.toml").write_text('[env]\nMY_FLAG = "on"\n', encoding="utf-8")
    r = _EnvCapturingRunner()
    sandbox.launch(r, "probe", tmp_path, agent="opencode", env_pairs=["MY_FLAG=override"])
    env = dict(line.split("=", 1) for line in r.env_snapshots[0])
    assert env["MY_FLAG"] == "override"  # CLI wins over [env]


def test_launch_claude_ignores_toml_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # claude's auth branch stays exactly as-is: [env] must NOT leak into its env-file.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")
    (tmp_path / "danno.toml").write_text('[env]\nMY_FLAG = "on"\n', encoding="utf-8")
    r = _EnvCapturingRunner()
    sandbox.launch(r, "probe", tmp_path, agent="claude")
    assert r.env_snapshots[0] == ["CLAUDE_CODE_OAUTH_TOKEN=tok"]
