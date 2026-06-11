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
import tempfile
from pathlib import Path

from ..core.exec import Runner, log_info
from . import ollama

DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434/v1"
DEFAULT_ALLOW_HOSTS = ("localhost:11434",)


def default_name(target_abs: Path) -> str:
    """Sandbox name derived from the project directory: `danno-<basename>`."""
    return f"danno-{target_abs.name}"


def create(runner: Runner, name: str, target_abs: Path) -> list[str]:
    """Create the sandbox, mounting the project at the same path inside the VM."""
    return runner.advise(
        ["docker", "sandbox", "create", "--name", name, "opencode", str(target_abs)],
        why=f"create the OpenCode sandbox '{name}' for {target_abs}",
    )


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
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
) -> list[list[str]]:
    """Get the sandbox to 'ready': create + egress hole + stop (so the policy applies
    on next start). Does NOT launch the TUI — that's `start`."""
    ollama.announce_loopback()
    if not (target_abs / ".opencode" / "opencode.jsonc").is_file():
        log_info(
            "[yellow]WARN[/yellow] target has no .opencode/opencode.jsonc — "
            "run `danno install` first so the sandbox has a config to load."
        )
    cmds = [
        create(runner, name, target_abs),
        configure_proxy(runner, name, allow_hosts),
        # The network policy only takes on a fresh VM start; stop so the next
        # `start` applies the allow-rule.
        stop(runner, name),
    ]
    return cmds


def _build_env_file(ollama_url: str, env_pairs: list[str], env_files: list[str]) -> Path:
    """Combine --env-file(s), --env pairs, and OLLAMA_BASE_URL into one 0600 temp file."""
    fd, path = tempfile.mkstemp(prefix="danno-env-")
    os.close(fd)
    p = Path(path)
    p.chmod(0o600)
    lines: list[str] = []
    for f in env_files:
        lines.append(Path(f).read_text(encoding="utf-8").rstrip("\n"))
    lines.extend(env_pairs)
    lines.append(f"OLLAMA_BASE_URL={ollama_url}")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def launch(
    runner: Runner,
    name: str,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    env_pairs: list[str] | None = None,
    env_files: list[str] | None = None,
) -> list[str]:
    """Launch the in-container OpenCode TUI, wired to host Ollama via an env-file."""
    env_pairs = env_pairs or []
    env_files = env_files or []
    log_info(f"injecting OLLAMA_BASE_URL={ollama_url} via a chmod-600 --env-file")
    if runner.apply and not runner.dry_run:
        env_path = _build_env_file(ollama_url, env_pairs, env_files)
        try:
            return runner.advise(
                ["docker", "sandbox", "exec", "-it", "--env-file", str(env_path), name, "opencode"],
                why=f"launch the OpenCode TUI in sandbox '{name}'",
            )
        finally:
            env_path.unlink(missing_ok=True)
    return runner.advise(
        ["docker", "sandbox", "exec", "-it", "--env-file", "<env-file>", name, "opencode"],
        why=f"launch the OpenCode TUI in sandbox '{name}'",
    )


def start(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
    env_pairs: list[str] | None = None,
    env_files: list[str] | None = None,
) -> None:
    """Provision (idempotent) then launch the in-container OpenCode TUI."""
    provision(runner, name, target_abs, allow_hosts=allow_hosts)
    launch(runner, name, ollama_url=ollama_url, env_pairs=env_pairs, env_files=env_files)


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
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
) -> list[list[str]]:
    """Recycle the sandbox: remove it, then re-provision from scratch."""
    rm = runner.advise(["docker", "sandbox", "rm", "-f", name], why=f"remove sandbox '{name}'")
    return [rm, *provision(runner, name, target_abs, allow_hosts=allow_hosts)]


def update(runner: Runner, name: str) -> list[str]:
    """Advise how to update OpenCode inside the container.

    OpenCode is provided by Docker Desktop's prebuilt `opencode` image, so the
    durable update path is recreating the sandbox on a newer image; this advises
    the in-container self-update as the quick path.
    """
    log_info(
        "OpenCode ships in Docker's prebuilt sandbox image; for a full update, "
        "`danno sandbox rebuild` after updating Docker Desktop."
    )
    return runner.advise(
        ["docker", "sandbox", "exec", name, "opencode", "upgrade"],
        why=f"update OpenCode inside sandbox '{name}'",
    )
