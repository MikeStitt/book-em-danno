"""Slow, opt-in live tests. Run with `pytest -m slow`.

These talk to the REAL host Ollama and sandboxes. The sandbox-driving tests use
`sbx` exclusively (never the danno CLI, never legacy `docker sandbox`) and skip
unless `sbx` is installed with its Docker runtime up, so the gate stays green on a
cold host. They NEVER invoke host `opencode` — OpenCode only ever runs in-container,
asserted via `sbx exec`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from book_em_danno.commands import ollama, sandbox, tools
from book_em_danno.config.generate import generate
from book_em_danno.config.schema import DannoConfig, Model, NpmPlugin, OllamaBackend, Tool
from book_em_danno.core.exec import Runner

pytestmark = pytest.mark.slow

# Real tags pulled on this host (see resume-phase2-execution memory).
TOOL_CAPABLE_MODEL = "gemma4:26b"

ollama_down = not ollama.reachable()
# The end-to-end tests below drive the sandbox directly (not via the danno CLI) and
# speak only `sbx`, so they need `sbx` on PATH with its Docker runtime up; otherwise
# they skip cleanly on a cold host.
sbx_down = shutil.which("sbx") is None or (
    subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0
)

# An ADOS checkout is needed for the opencode+ados permutation; skip if absent.
try:
    ADOS_REPO: str | None = str(tools.resolve_ados_repo())
except tools.ToolInstallError:
    ADOS_REPO = None


@pytest.mark.skipif(ollama_down, reason="Ollama not reachable (start: OLLAMA_HOST=0.0.0.0 serve)")
def test_model_responds() -> None:
    assert ollama.verify_responds(TOOL_CAPABLE_MODEL)


@pytest.mark.skipif(ollama_down, reason="Ollama not reachable")
def test_model_can_tool_call() -> None:
    assert ollama.tool_call_probe(TOOL_CAPABLE_MODEL), (
        f"{TOOL_CAPABLE_MODEL} did not emit tool_calls — it is unusable for ADOS agents"
    )


def _teardown_sandbox(name: str) -> None:
    # sbx-only: every test in this file creates via `sbx`. `sbx rm` needs `--force` in a
    # headless shell and won't remove a running VM, so stop first. Best-effort (the
    # sandbox may not exist).
    subprocess.run(["sbx", "stop", name], capture_output=True, check=False)
    subprocess.run(["sbx", "rm", "--force", name], capture_output=True, check=False)


def _assert_harness_version_in_sandbox(name: str, harness: str, workspace: Path) -> None:
    # The sbx image and the in-VM binary share the harness name (opencode / claude).
    # `sbx create` needs the one-time global network policy; init it (idempotent — a
    # no-op once initialized). Then create the sandbox and read `<harness> --version`
    # entirely via `sbx`.
    sandbox.ensure_policy_initialized(Runner(apply=True))
    created = subprocess.run(
        ["sbx", "create", "--name", name, harness, str(workspace)],
        capture_output=True,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    ver = subprocess.run(
        ["sbx", "exec", name, harness, "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ver.returncode == 0 and ver.stdout.strip()


@pytest.mark.skipif(sbx_down, reason="sbx not installed or its Docker runtime is down")
def test_opencode_only_runs_in_container(tmp_path: Path) -> None:
    # A throwaway sandbox proves OpenCode is reachable in-container — never on host.
    name = "danno-livetest"
    _teardown_sandbox(name)
    try:
        _assert_harness_version_in_sandbox(name, "opencode", tmp_path)
    finally:
        _teardown_sandbox(name)


@pytest.mark.skipif(sbx_down, reason="sbx not installed or its Docker runtime is down")
def test_claudecode_runs_in_container(tmp_path: Path) -> None:
    # Claude Code is a prebuilt harness like opencode; prove it runs in-container.
    # Auth-free (`--version` only) so it's green on a cold host.
    name = "danno-claudetest"
    _teardown_sandbox(name)
    try:
        _assert_harness_version_in_sandbox(name, "claude", tmp_path)
    finally:
        _teardown_sandbox(name)


def _npm_demo_config() -> DannoConfig:
    """A minimal config that drives an [[npm]] plugin through the real chain:
    danno.toml -> generated opencode.jsonc "plugin" array -> OpenCode auto-install.
    The agent never runs a model (we only `opencode agent list`), so no pull/auth.

    We deliberately use only `opencode-planner` — a plugin Bun installs straight from
    npm. The `@plannotator/opencode` plugin's `setup` step curls plannotator.ai, which
    the sandbox egress proxy denies (403, only the Ollama host is allow-listed); see the
    commented block in danno.toml.example. Its `setup` path is covered by unit tests."""
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1")
        },
        models={
            "gemma": Model(
                backend="ollama",
                tag=TOOL_CAPABLE_MODEL,
                context_budget=32000,
                output_limit=8192,
            )
        },
        agents={"build": "gemma"},
        npm=[NpmPlugin(package="opencode-planner")],
    )


@pytest.mark.skipif(sbx_down, reason="sbx not installed or its Docker runtime is down")
def test_npm_plugins_install_in_container(tmp_path: Path) -> None:
    """opencode+opencode-planner (the npm part of perms 3/4).

    Exercises the danno-owned chain end to end: generate the opencode.jsonc plugin
    array, provision a real sandbox, run any plugin `setup`, then trigger OpenCode so
    Bun auto-installs the plugin — and assert the exact in-container artifact observed
    during interactive validation. (The plannotator plugin is omitted: its `setup` curls
    plannotator.ai, which the sandbox egress proxy denies; see `_npm_demo_config`.)
    """
    target = tmp_path
    generate(_npm_demo_config(), target, apply=True)
    runner = Runner(apply=True)
    name = "danno-livetest-npm"
    _teardown_sandbox(name)
    try:
        sandbox.provision(runner, name, target)  # create + egress allow + stop
        sandbox.run_npm_setup(runner, name, _npm_demo_config().npm)  # plugin setup steps
        # `opencode agent list` loads the project config + plugins, which makes Bun
        # install the npm plugins into ~/.cache/opencode/packages. `sbx exec` auto-starts
        # the VM that `provision` stopped. (`provision` also creates via sbx here — the
        # slow-test conftest clears DANNO_SANDBOX_CLI, so auto-detect picks the installed
        # sbx; a sandbox is invisible to the OTHER backend's exec — "VM not found".)
        trigger = f"cd {target} && opencode agent list"
        subprocess.run(
            ["sbx", "exec", name, "bash", "-lc", trigger],
            capture_output=True,
            check=False,
            timeout=420,
        )
        probe = "test -d ~/.cache/opencode/packages/opencode-planner@latest && echo OK"
        result = subprocess.run(
            ["sbx", "exec", name, "bash", "-lc", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        assert result.stdout.strip() == "OK", (
            f"missing plugin/setup artifacts in-container:\n{result.stdout}\n{result.stderr}"
        )
    finally:
        _teardown_sandbox(name)


@pytest.mark.skipif(ADOS_REPO is None, reason="no ADOS checkout (set ADOS_REPO or --ados-repo)")
def test_ados_installs_project_local(tmp_path: Path) -> None:
    """opencode+ados (perm 2). danno runs ADOS's --local installer in the target and
    copies its agent/command defs project-local (the sandbox can't see the host's
    global ~/.config/opencode), recording provenance. Assert that danno-owned outcome.

    No Docker needed — this is a host-side, project-local copy under --apply."""
    assert ADOS_REPO is not None  # narrowed for type-checker; guarded by skipif
    # ADOS's --local installer refuses to run outside a git work tree.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    tool = Tool(name="ados", source=ADOS_REPO, install_to="sandbox")
    tools.install_ados(Runner(apply=True), tool, tmp_path, ados_repo=ADOS_REPO)
    agent_defs = list((tmp_path / ".opencode" / "agent").glob("*.md"))
    command_defs = list((tmp_path / ".opencode" / "command").glob("*.md"))
    assert agent_defs, "ADOS agent defs not copied project-local"
    assert command_defs, "ADOS command defs not copied project-local"
    assert (tmp_path / ".opencode" / "ados-provenance.txt").is_file()
