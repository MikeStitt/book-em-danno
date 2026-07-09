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
