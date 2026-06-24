"""Claurst agent-under-test: install it into a sandbox and drive the sweep with it.

Claurst (`Kuberwastaken/claurst`, a pure-Rust Claude-Code clone) is NOT one of the
prebuilt `docker sandbox` images, so the validator hosts it in a `shell` sandbox
and installs the release binary post-provision. The install MUST curl-fetch the
tarball: `npm i -g claurst`'s postinstall connects DIRECT to GitHub and the sandbox
(proxy-only egress) rejects it (`ECONNREFUSED`), whereas curl is proxy-aware (M0
spike, 2026-06-23). The sweep then drives claurst via `driver.claurst_run`, which
stands up the per-turn Ollama relay; this module only supplies the install step and
the `TurnFn` seam, so `run_sweep`/`run_tiers`/the oracle stay unchanged.

Scope: claurst is wired for **local Ollama** models. Its Rust client ignores the
proxy, so cloud-backed variants in the matrix cannot reach their providers and will
error in their own row (fail loud, visible in the report) rather than be silently
skipped.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from book_em_danno.commands.sandbox import exec_in_container
from book_em_danno.core.exec import Runner
from danno_validator.driver import (
    CLAURST_MODEL_FLAG,
    CLAURST_OLLAMA_HOST,
    Turn,
    TurnFn,
    _claurst_script,
    claurst_run,
)

# The prebuilt sandbox image that hosts claurst. `shell` carries the toolchain the
# driver needs (python3 for the relay, curl, git, tar) — verified in the M0 spike.
CLAURST_SANDBOX_IMAGE = "shell"

# Pinned release. Bump deliberately (and re-pin the stream-json schema in driver.py
# against the new version — M1 discipline). aarch64 = the Docker Desktop microVM.
CLAURST_VERSION = "0.1.5"
CLAURST_RELEASE_URL = (
    f"https://github.com/kuberwastaken/claurst/releases/download/"
    f"v{CLAURST_VERSION}/claurst-linux-aarch64.tar.gz"
)


def install_claurst(runner: Runner, sandbox: str) -> list[str]:
    """Install the claurst binary into `sandbox` (curl-fetched, idempotent).

    Drops the binary into `~/.local/bin` (already first on PATH in the shell VM).
    Skips the work when claurst is already present AND runs (so a `--keep-sandboxes`
    re-run is cheap, but a half-installed VM still gets repaired). A clean shell VM
    lacks `libasound.so.2`, which the claurst binary links, so the ALSA runtime is
    apt-installed first (`sudo -E` keeps the proxy env; verified 2026-06-23). Fails
    loud (CommandFailedError via `exec_in_container` under --apply) if any step
    fails. Returns the exec command for inspection.
    """
    script = (
        # Skip only if claurst is present and actually runs (libs + binary OK).
        "command -v claurst >/dev/null 2>&1 && claurst --version >/dev/null 2>&1 "
        "&& { claurst --version; exit 0; }; "
        "set -e; "
        # claurst links libasound; install the ALSA runtime (t64 on Ubuntu 24.04+,
        # plain libasound2 on older). apt reaches the index through the proxy.
        "sudo -E apt-get update -qq; "
        "sudo -E apt-get install -y -qq libasound2t64 "
        "|| sudo -E apt-get install -y -qq libasound2; "
        'd=$(mktemp -d); cd "$d"; '
        f"curl -fsSL --max-time 180 -o claurst.tgz {CLAURST_RELEASE_URL}; "
        "tar xzf claurst.tgz; mkdir -p ~/.local/bin; "
        'install -m 0755 "$(find . -name claurst -type f | head -1)" ~/.local/bin/claurst; '
        "claurst --version"
    )
    return exec_in_container(
        runner, sandbox, script, why=f"install claurst v{CLAURST_VERSION} in sandbox '{sandbox}'"
    )


def interactive_launch_script(model_ref: str | None, passthru: list[str]) -> list[str]:
    """`container_argv` for an INTERACTIVE claurst session — the `danno sandbox start
    --agent claurst` counterpart of headless `claurst_run`.

    Returns `["bash", "-lc", <script>]` where the script is the SAME Ollama-relay
    bracket the headless path uses (`driver._claurst_script`: relay backgrounded on
    127.0.0.1:11434, reaped via `trap … EXIT`) wrapped around a TTY claurst run — no
    `-p`, so claurst opens its interactive UI. The relay lives exactly as long as this
    single long-running `docker sandbox exec` (the whole session), which is why no
    persistent daemon is needed; the headless per-turn path is reused unchanged.

    `model_ref` is claurst's `-m ollama/<tag>` (already resolved + locality-checked by
    the caller); `passthru` is the agent's `--`-forwarded args, verbatim."""
    argv = ["claurst"]
    if model_ref is not None:
        argv += [CLAURST_MODEL_FLAG, model_ref]
    argv += passthru
    claurst_cmd = f"OLLAMA_HOST={CLAURST_OLLAMA_HOST} {shlex.join(argv)}"
    return ["bash", "-lc", _claurst_script(claurst_cmd)]


def authed_claurst_run(env_file: Path | None) -> TurnFn:
    """A `TurnFn` that drives `claurst_run` with `env_file` bound, for `run_sweep`.

    Mirrors `sweep._authed_opencode_run` so the level runners just call a plain
    `TurnFn`. Local Ollama claurst needs no auth; `env_file` is forwarded to the
    exec for matrix parity (cloud configs) and is harmless when None. The Ollama
    relay is set up inside `claurst_run` itself, per turn.
    """

    def run(
        runner: Runner,
        name: str,
        prompt: str,
        *,
        session: str | None = None,
        agent: str | None = None,
        model: str | None = None,
        skip_permissions: bool = False,
        workspace: str | Path | None = None,
    ) -> Turn:
        return claurst_run(
            runner,
            name,
            prompt,
            session=session,
            agent=agent,
            model=model,
            skip_permissions=skip_permissions,
            workspace=workspace,
            env_file=env_file,
        )

    return run
