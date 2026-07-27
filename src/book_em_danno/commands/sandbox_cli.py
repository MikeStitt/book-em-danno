"""Sandbox CLI backend seam: `sbx` (default) or legacy `docker sandbox`.

Docker deprecated the Desktop-integrated `docker sandbox` subcommand in favor of
the standalone `sbx` binary. danno auto-prefers `sbx` when it is on PATH and falls
back to `docker sandbox`; set `DANNO_SANDBOX_CLI=sbx|docker` to force one. Every
sandbox argv is built through here so no other module hardcodes the CLI.

Command mapping (verified against `sbx v0.34.0`, 2026-07-09):
- create / exec / ls / stop / rm / version: same verbs, different prefix
  (`base()`). `sbx exec` matches `docker exec` flags (`-e`, `-i`, `-t`, `-w`, ...),
  and the agents `shell`/`claude`/`opencode`/`codex` exist under both. NOTE: sbx
  v0.34.0's `exec --env-file` is a SILENT NO-OP (docker's works) — danno forwards env
  by name instead; see `env_forward_argv`.
- network egress differs — see `policy_allow_argv`.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_ENV = "DANNO_SANDBOX_CLI"

# SBX-TRANSITION(docker-sandbox-deprecation): explicit backend chosen by the CLI
# layer from `[sandbox].cli` / `--sandbox-cli` (flag>env>config resolved there).
# None leaves auto-detection. This whole selection machinery — and the `docker`
# branch of every builder — is removable once `docker sandbox` is gone.
_override: str | None = None


def set_backend(value: str | None) -> None:
    """Set the sandbox backend from `[sandbox].cli` / `--sandbox-cli` ('auto'|'sbx'|
    'docker'). 'auto' / None / unknown leaves auto-detection in place. Called once by
    the CLI layer before provisioning; `resolve_backend` returns it."""
    global _override
    _override = value if value in ("sbx", "docker") else None


def resolve_backend() -> str:
    """`'sbx'` or `'docker'`. Precedence: env `DANNO_SANDBOX_CLI` (debug/ad-hoc) >
    `set_backend()` (the CLI layer's flag>config) > auto-prefer `sbx` when installed.

    Fails loud (ValueError) on an invalid env value so a typo can't silently pick a
    backend. Absence of both CLIs is NOT checked here — argv construction must work
    in advise mode where nothing is installed; `doctor` surfaces availability.
    """
    env = os.environ.get(_ENV, "").strip().lower()
    if env in ("sbx", "docker"):
        return env
    if env:
        raise ValueError(f"{_ENV}={env!r} is invalid — use 'sbx' or 'docker'.")
    if _override in ("sbx", "docker"):
        return _override
    return "sbx" if shutil.which("sbx") else "docker"


def base() -> list[str]:
    """The CLI prefix: `['sbx']` or `['docker', 'sandbox']`."""
    return ["sbx"] if resolve_backend() == "sbx" else ["docker", "sandbox"]


def label() -> str:
    """Human label for the active backend (doctor / logs), e.g. `sbx`."""
    return " ".join(base())


def env_forward_argv(
    env_file: str | os.PathLike[str] | None,
) -> tuple[list[str], dict[str, str] | None]:
    """Expand a danno env-file into `exec -e NAME` flags + the subprocess env carrying
    the values. Returns `([], None)` for no env-file (inherit the caller's env unchanged).

    WHY NOT `--env-file`: sbx v0.34.0's `exec --env-file` is a SILENT NO-OP — it injects
    nothing into the sandbox (empirically confirmed: even a fresh var comes back unset) —
    AND sbx bakes cloud-key placeholders (`OPENAI_API_KEY=proxy-managed`, one per provider)
    into every sandbox for its own egress-proxy key-swap. danno routes cloud traffic through
    its OWN capture proxy, so that swap never fires and the placeholder reaches the provider
    verbatim → 401 (issue #99). A value-less `-e NAME` tells sbx to read NAME from THIS
    process's environment, which OVERRIDES the baked placeholder — so danno forwards each
    var by NAME and supplies the values through the exec subprocess's own environment,
    keeping every secret VALUE off the argv (host `ps`/shell history). Only the var name
    rides the command line.

    The returned env is the FULL `os.environ` merged with the file's values (never just the
    overlay), because `subprocess` `env=` REPLACES the environment rather than extending it —
    the sandbox CLI still needs `PATH` and friends.

    Legacy `docker sandbox exec` honored `--env-file`, but forwarding by name works there
    too (docker `-e NAME` reads from the caller env identically), so this path is uniform
    across both backends.
    """
    if env_file is None:
        return [], None
    values: dict[str, str] = {}
    for raw in Path(env_file).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val
    if not values:
        return [], None
    flags = [tok for key in values for tok in ("-e", key)]
    return flags, {**os.environ, **values}


def availability_argv() -> list[str]:
    """A cheap 'is the active CLI present' probe: `<base> version`."""
    return [*base(), "version"]


def rm_argv(name: str) -> list[str]:
    """Remove a sandbox. `sbx rm` prompts for confirmation and aborts on a non-tty
    ("stdin is not a terminal") — danno's exec path is headless — so it needs
    `--force`; legacy `docker sandbox rm` takes no force flag."""
    if resolve_backend() == "sbx":
        return ["sbx", "rm", "--force", name]
    return ["docker", "sandbox", "rm", name]


def policy_init_argv() -> list[str] | None:
    """Argv to initialize the sbx global network policy, or `None` for docker
    (which has no such one-time step). `sbx create` FAILS until the global policy
    exists; `balanced` = a safe default-deny base + common dev services (danno's
    per-sandbox `policy_allow_argv` then opens what its sandbox needs). Init is NOT
    idempotent, so callers must gate on `sbx policy ls` succeeding first."""
    if resolve_backend() != "sbx":
        return None
    return ["sbx", "policy", "init", "balanced"]


def policy_ls_argv() -> list[str]:
    """`sbx policy ls` — succeeds once the global policy is initialized, errors
    otherwise (the detection signal for `policy_init_argv`)."""
    return ["sbx", "policy", "ls"]


def ls_names_argv() -> tuple[list[str], bool]:
    """Argv that lists sandbox names, and whether the output is *quiet* (one bare
    name per line, no header). `sbx ls -q` is quiet — empty output when there are
    no sandboxes; legacy `docker sandbox ls` prints a header + table (skip row 0,
    take the first column). This avoids mis-reading `sbx`'s empty-state prose
    ("No sandboxes found.") as a sandbox named "Launch"."""
    if resolve_backend() == "sbx":
        return ["sbx", "ls", "-q"], True
    return ["docker", "sandbox", "ls"], False


def policy_allow_argv(name: str, allow_hosts: tuple[str, ...]) -> list[str]:
    """Egress-allow argv for sandbox `name` — allow ONLY the enumerated hosts.

    danno's whole purpose is isolating the AI from the host/LAN. The contract:
    the sandbox may reach the internet-egress its base policy permits, but the
    host/LAN is DENIED except the explicit Ollama hole in `allow_hosts`. NEVER
    allow `"**"` (that would expose the host, the LAN, and cloud metadata — see the
    `sandbox-security-contract-fail-loud` memory).

    `allow_hosts` entries are `host:port` and are passed through VERBATIM — the
    caller supplies the sandbox-reachable form. Both backends' proxies rewrite
    `host.docker.internal`→`localhost` before matching, so a same-host Ollama uses the
    default `localhost:11434` token on both; a remote Ollama is its real IP:port (see
    `configure_proxy`).

    - `sbx`: `policy allow network --sandbox N <h1,h2,…>` on the `balanced` base
      (default-deny + curated dev/AI hosts; see `ensure_policy_initialized`).
      Enforcement is via the host HTTP(S) proxy; a denied host returns 403.
    - legacy `docker sandbox`: `network proxy N --policy allow --allow-host H…`.
    """
    if resolve_backend() == "sbx":
        return ["sbx", "policy", "allow", "network", "--sandbox", name, ",".join(allow_hosts)]
    cmd = ["docker", "sandbox", "network", "proxy", name, "--policy", "allow"]
    for host in allow_hosts:
        cmd += ["--allow-host", host]
    return cmd
