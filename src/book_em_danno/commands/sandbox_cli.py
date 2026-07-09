"""Sandbox CLI backend seam: `sbx` (default) or legacy `docker sandbox`.

Docker deprecated the Desktop-integrated `docker sandbox` subcommand in favor of
the standalone `sbx` binary. danno auto-prefers `sbx` when it is on PATH and falls
back to `docker sandbox`; set `DANNO_SANDBOX_CLI=sbx|docker` to force one. Every
sandbox argv is built through here so no other module hardcodes the CLI.

Command mapping (verified against `sbx v0.34.0`, 2026-07-09):
- create / exec / ls / stop / rm / version: same verbs, different prefix
  (`base()`). `sbx exec` matches `docker exec` flags (`--env-file`, `-i`, `-t`,
  `-w`, ...), and the agents `shell`/`claude`/`opencode`/`codex` exist under both.
- network egress differs — see `policy_allow_argv`.
"""

from __future__ import annotations

import os
import shutil

_ENV = "DANNO_SANDBOX_CLI"


def resolve_backend() -> str:
    """`'sbx'` or `'docker'`. `DANNO_SANDBOX_CLI` overrides; else auto-prefer `sbx`.

    Fails loud (ValueError) on an invalid override so a typo can't silently pick a
    backend. Absence of both CLIs is NOT checked here — argv construction must work
    in advise mode where nothing is installed; `doctor` is where availability is
    surfaced.
    """
    override = os.environ.get(_ENV, "").strip().lower()
    if override in ("sbx", "docker"):
        return override
    if override:
        raise ValueError(f"{_ENV}={override!r} is invalid — use 'sbx' or 'docker'.")
    return "sbx" if shutil.which("sbx") else "docker"


def base() -> list[str]:
    """The CLI prefix: `['sbx']` or `['docker', 'sandbox']`."""
    return ["sbx"] if resolve_backend() == "sbx" else ["docker", "sandbox"]


def label() -> str:
    """Human label for the active backend (doctor / logs), e.g. `sbx`."""
    return " ".join(base())


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
    """Egress-allow argv for sandbox `name`.

    Legacy `docker sandbox`: `network proxy N --policy allow [--allow-host H]…`
    (allow internet; deny host/LAN except the enumerated holes).

    `sbx`: `policy allow network --sandbox N "**"` — allow ALL egress for the
    sandbox. This is broader than the legacy LAN-deny posture; tightening it to
    sbx base profiles / deny-rules is tracked as P3 hardening in the sbx-migration
    plan (`allow_hosts` is retained for that future per-host form).
    """
    if resolve_backend() == "sbx":
        return ["sbx", "policy", "allow", "network", "--sandbox", name, "**"]
    cmd = ["docker", "sandbox", "network", "proxy", name, "--policy", "allow"]
    for host in allow_hosts:
        cmd += ["--allow-host", host]
    return cmd
