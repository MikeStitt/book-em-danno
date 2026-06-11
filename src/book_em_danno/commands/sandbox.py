"""Docker sandbox lifecycle (port of `tools/ados-sandbox`).

OpenCode runs ONLY inside the Docker Desktop microVM sandbox (a hard security
invariant — danno never invokes host `opencode`). The sandbox reaches host Ollama
through the egress proxy's allow-rule; the proxy rewrites host.docker.internal →
localhost, so the rule names `localhost:11434`.

Every operation goes through `Runner.advise` (Tier-2: advise by default, execute
under --apply), so the exact `docker sandbox …` commands are inspectable and
testable without a daemon.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from ..core.exec import CommandFailedError, Runner, log_info
from . import ollama

DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434/v1"
DEFAULT_ALLOW_HOSTS = ("localhost:11434",)
DEFAULT_AGENT = "opencode"
# Auth env vars Claude Code accepts, in preference order (subscription token first).
CLAUDE_AUTH_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def default_name(target_abs: Path, agent: str = DEFAULT_AGENT) -> str:
    """Sandbox name derived from the project directory.

    `danno-<basename>` for the default opencode agent (back-compat); a per-agent
    suffix otherwise so agents get separate sandboxes that can coexist.
    """
    base = f"danno-{target_abs.name}"
    return base if agent == DEFAULT_AGENT else f"{base}-{agent}"


def sandbox_exists(name: str) -> bool:
    """True if a sandbox named `name` is listed by `docker sandbox ls`."""
    try:
        out = subprocess.run(
            ["docker", "sandbox", "ls"], capture_output=True, text=True, check=False
        ).stdout
    except (FileNotFoundError, OSError):
        return False
    return any(line.split() and line.split()[0] == name for line in out.splitlines()[1:])


def _live(runner: Runner) -> bool:
    """True when we are actually executing (so existence checks are meaningful)."""
    return runner.apply and not runner.dry_run


def create(runner: Runner, name: str, target_abs: Path, agent: str = DEFAULT_AGENT) -> list[str]:
    """Create the sandbox, mounting the project at the same path inside the VM.

    `agent` selects the Docker prebuilt agent baked into the VM (opencode, claude,
    …). Idempotent under --apply: an already-existing sandbox is left in place.
    """
    cmd = ["docker", "sandbox", "create", "--name", name, agent, str(target_abs)]
    if _live(runner) and sandbox_exists(name):
        log_info(f"sandbox '{name}' already exists — skipping create")
        return cmd
    return runner.advise(cmd, why=f"create the {agent} sandbox '{name}' for {target_abs}")


def configure_proxy(
    runner: Runner, name: str, allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS
) -> list[str]:
    """Open general internet egress but keep host/LAN denied except the Ollama hole."""
    cmd = ["docker", "sandbox", "network", "proxy", name, "--policy", "allow"]
    for host in allow_hosts:
        cmd += ["--allow-host", host]
    return runner.advise(
        cmd, why="set the egress policy (internet allowed; host/LAN denied except Ollama)"
    )


def stop(runner: Runner, name: str) -> list[str]:
    """Stop the sandbox VM (also how a fresh network policy is made to take effect)."""
    return runner.advise(["docker", "sandbox", "stop", name], why=f"stop sandbox '{name}'")


def provision(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    agent: str = DEFAULT_AGENT,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
) -> list[list[str]]:
    """Get the sandbox to 'ready': create + egress hole + stop (so the policy applies
    on next start). Does NOT launch the TUI — that's `start`."""
    ollama.announce_loopback()
    if agent == DEFAULT_AGENT and not (target_abs / ".opencode" / "opencode.jsonc").is_file():
        log_info(
            "[yellow]WARN[/yellow] target has no .opencode/opencode.jsonc — "
            "run `danno install` first so the sandbox has a config to load."
        )
    cmds = [
        create(runner, name, target_abs, agent),
        configure_proxy(runner, name, allow_hosts),
        # The network policy only takes on a fresh VM start; stop so the next
        # `start` applies the allow-rule.
        stop(runner, name),
    ]
    return cmds


def agent_env(agent: str, ollama_url: str) -> list[str]:
    """The agent-specific `KEY=VAL` env-file lines.

    opencode reaches host Ollama via OLLAMA_BASE_URL. claude needs auth: prefer the
    subscription token (CLAUDE_CODE_OAUTH_TOKEN), else ANTHROPIC_API_KEY, read from
    danno's host environment. Fail loud (Working Rule 8) when neither is set. The
    secret only ever lands in the chmod-600 env-file, never on the command line.
    """
    if agent == "claude":
        for var in CLAUDE_AUTH_VARS:
            val = os.environ.get(var)
            if val:
                return [f"{var}={val}"]
        raise CommandFailedError(
            "Claude Code needs auth but neither CLAUDE_CODE_OAUTH_TOKEN nor "
            "ANTHROPIC_API_KEY is set in danno's environment. Run `claude setup-token` "
            "(Max/Pro subscription) and export CLAUDE_CODE_OAUTH_TOKEN, or export "
            "ANTHROPIC_API_KEY for API billing."
        )
    return [f"OLLAMA_BASE_URL={ollama_url}"]


def _build_env_file(agent_lines: list[str], env_pairs: list[str], env_files: list[str]) -> Path:
    """Combine --env-file(s), --env pairs, and the agent env lines into one 0600 temp file."""
    fd, path = tempfile.mkstemp(prefix="danno-env-")
    os.close(fd)
    p = Path(path)
    p.chmod(0o600)
    lines: list[str] = []
    for f in env_files:
        lines.append(Path(f).read_text(encoding="utf-8").rstrip("\n"))
    lines.extend(env_pairs)
    lines.extend(agent_lines)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def launch(
    runner: Runner,
    name: str,
    *,
    agent: str = DEFAULT_AGENT,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    env_pairs: list[str] | None = None,
    env_files: list[str] | None = None,
) -> list[str]:
    """Launch the in-container agent, wired to host Ollama / agent auth via an env-file."""
    env_pairs = env_pairs or []
    env_files = env_files or []
    if runner.apply and not runner.dry_run:
        lines = agent_env(agent, ollama_url)
        injected = ", ".join(line.split("=", 1)[0] for line in lines)
        log_info(f"injecting {injected} via a chmod-600 --env-file")
        env_path = _build_env_file(lines, env_pairs, env_files)
        try:
            return runner.advise(
                ["docker", "sandbox", "exec", "-it", "--env-file", str(env_path), name, agent],
                why=f"launch {agent} in sandbox '{name}'",
            )
        finally:
            env_path.unlink(missing_ok=True)
    log_info(f"would inject {agent} env (OLLAMA_BASE_URL / auth token) via a chmod-600 --env-file")
    return runner.advise(
        ["docker", "sandbox", "exec", "-it", "--env-file", "<env-file>", name, agent],
        why=f"launch {agent} in sandbox '{name}'",
    )


def start(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    agent: str = DEFAULT_AGENT,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
    env_pairs: list[str] | None = None,
    env_files: list[str] | None = None,
) -> None:
    """Provision (idempotent) then launch the in-container agent."""
    provision(runner, name, target_abs, agent=agent, allow_hosts=allow_hosts)
    launch(
        runner, name, agent=agent, ollama_url=ollama_url, env_pairs=env_pairs, env_files=env_files
    )


def shell(runner: Runner, name: str) -> list[str]:
    """Open an interactive bash shell inside the sandbox VM."""
    return runner.advise(
        ["docker", "sandbox", "exec", "-it", name, "bash"], why=f"open a shell in sandbox '{name}'"
    )


def rebuild(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    agent: str = DEFAULT_AGENT,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
) -> list[list[str]]:
    """Recycle the sandbox: remove it (if present), then re-provision from scratch.

    `docker sandbox rm` takes no force flag and errors on a missing sandbox, so
    under --apply we stop-then-remove only when it actually exists.
    """
    cmds: list[list[str]] = []
    if not _live(runner) or sandbox_exists(name):
        # Stop first so rm doesn't trip on a running VM, then remove.
        cmds.append(stop(runner, name))
        cmds.append(
            runner.advise(["docker", "sandbox", "rm", name], why=f"remove sandbox '{name}'")
        )
    cmds += provision(runner, name, target_abs, agent=agent, allow_hosts=allow_hosts)
    return cmds


def update(runner: Runner, name: str, agent: str = DEFAULT_AGENT) -> list[str]:
    """Advise how to update the agent inside the container.

    The agent ships in Docker Desktop's prebuilt sandbox image, so the durable
    update path is recreating the sandbox on a newer image; this advises the
    in-container self-update as the quick path.
    """
    if agent == "claude":
        log_info(
            "Claude Code ships in Docker's prebuilt sandbox image; for a full update, "
            "`danno sandbox rebuild --agent claude` after updating Docker Desktop."
        )
        return runner.advise(
            ["docker", "sandbox", "exec", name, "claude", "update"],
            why=f"update Claude Code inside sandbox '{name}'",
        )
    log_info(
        "OpenCode ships in Docker's prebuilt sandbox image; for a full update, "
        "`danno sandbox rebuild` after updating Docker Desktop."
    )
    return runner.advise(
        ["docker", "sandbox", "exec", name, "opencode", "upgrade"],
        why=f"update OpenCode inside sandbox '{name}'",
    )
