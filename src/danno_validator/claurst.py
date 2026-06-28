"""Claurst agent-under-test: install it into a sandbox and drive the sweep with it.

Claurst (a pure-Rust Claude-Code clone) is NOT one of the prebuilt `docker sandbox`
images, so the validator hosts it in a `shell` sandbox and installs the release
binary post-provision. danno ships the **danno-pinned fork build**
(`MikeStitt/claurst`, the `danno-integration` branch): the upstream Bug 1/4/5/6/7
fixes (stall timeout, context window/meter, zero-tools warning) plus env-gated,
danno-only diagnostics — see `.docs/claurst-fork-and-first-class-integration-plan.md`.
The install MUST curl-fetch the tarball: `npm i -g claurst`'s postinstall connects
DIRECT to GitHub and the sandbox (proxy-only egress) rejects it (`ECONNREFUSED`),
whereas curl is proxy-aware (M0 spike, 2026-06-23). The sweep then drives claurst via
`driver.claurst_run`, which stands up the per-turn Ollama relay; this module only
supplies the install step and the `TurnFn` seam, so `run_sweep`/`run_tiers`/the oracle
stay unchanged.

Scope: claurst runs **local Ollama** and the cloud providers danno can fully wire
(today NVIDIA NIM). The fork build honors the sandbox egress proxy, so a cloud model
selected via `-m` is reached directly through it with the provider key injected from
the backend's `api_key_env` (see `sandbox.resolve_claurst_model` /
`claurst_cloud_env_lines`); an unmapped cloud host or a raw non-Ollama ref still fails
loud rather than launching to a silent mid-session failure. NOTE: cloud requires the
danno fork build (it honors the proxy); the pinned binary here must be that build.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from book_em_danno.commands.sandbox import exec_in_container
from book_em_danno.core.exec import Runner
from danno_validator.driver import (
    CLAURST_MODEL_FLAG,
    CLAURST_OLLAMA_HOST,
    CLAURST_RELAY_DEFAULT_UPSTREAM_PORT,
    Turn,
    TurnFn,
    _claurst_script,
    claurst_run,
)

# The prebuilt sandbox image that hosts claurst. `shell` carries the toolchain the
# driver needs (python3 for the relay, curl, git, tar) — verified in the M0 spike.
CLAURST_SANDBOX_IMAGE = "shell"

# Pinned fork release on `MikeStitt/claurst` (the `danno-integration` build, tagged
# `v<version>`). Bump deliberately (and re-pin the stream-json schema in driver.py
# against the new version — M1 discipline). aarch64 = the Docker Desktop microVM.
# The `-danno<N>` suffix marks the fork build: upstream v0.1.6 (bash-tool detached-child
# hang fix, claurst PR #189 / issue #184) PLUS the Bug 1/4/5/6/7 fixes + danno-only
# diagnostics. NOTE: the binary self-reports the bare Cargo version ("0.1.6"), so the
# `-danno<N>` suffix lives only in this pin and the release tag, never in `--version`;
# the install skip is therefore gated on a danno-written stamp file, not `--version`.
CLAURST_VERSION = "0.1.6-danno1"
CLAURST_RELEASE_URL = (
    f"https://github.com/MikeStitt/claurst/releases/download/"
    f"v{CLAURST_VERSION}/claurst-linux-aarch64.tar.gz"
)
# Records which danno pin is installed, so the skip can distinguish the fork build
# (and future `-danno<N>` bumps) from upstream / an older binary that all report the
# same bare `--version`. Lives under the per-user data dir inside the sandbox HOME.
CLAURST_VERSION_STAMP = "$HOME/.local/share/danno/claurst-version"


def install_claurst(runner: Runner, sandbox: str) -> list[str]:
    """Install the claurst binary into `sandbox` (curl-fetched, idempotent).

    Drops the binary into `~/.local/bin` (already first on PATH in the shell VM).
    Skips the work only when the danno-pinned build is already stamped (the stamp
    file equals `CLAURST_VERSION`) AND the binary still runs — so a `--keep-sandboxes`
    re-run is cheap, but a bare upstream binary (no stamp) or an older danno pin forces
    an upgrade reinstall instead of silently keeping it. The stamp, not `--version`, is
    the gate because the fork binary self-reports the bare Cargo version and so cannot
    be told apart from upstream by `--version` alone. A clean shell VM lacks
    `libasound.so.2`, which the claurst binary links, so the ALSA runtime is apt-installed
    first (`sudo -E` keeps the proxy env; verified 2026-06-23). Fails loud
    (CommandFailedError via `exec_in_container` under --apply) if any step fails.
    Returns the exec command for inspection.

    The release download is **resumable + retried** (`--retry --retry-all-errors -C -`):
    the squid egress proxy intermittently truncates the GitHub-CDN HTTPS transfer
    (`curl: (18) transfer closed`), which a single shot cannot survive — the resume
    picks up the partial file and completes (observed 2026-06-26).
    """
    script = (
        # Skip only if claurst is present AND the danno-pinned build is already stamped
        # (so a version bump upgrades an existing sandbox instead of keeping the old
        # binary). The fork binary self-reports the bare Cargo version, so a danno-written
        # stamp file — not `--version` — is what distinguishes it from upstream / an older
        # pin. A bare upstream binary (no stamp) or a stale pin falls through to reinstall.
        f'stamp="{CLAURST_VERSION_STAMP}"; '
        "command -v claurst >/dev/null 2>&1 "
        f'&& [ "$(cat "$stamp" 2>/dev/null)" = "{CLAURST_VERSION}" ] '
        "&& claurst --version >/dev/null 2>&1 "
        "&& { claurst --version; exit 0; }; "
        "set -e; "
        # claurst links libasound.so.2; install the ALSA runtime (t64 on Ubuntu 24.04+,
        # plain libasound2 on older) ONLY if it's missing. Skipping apt when the lib is
        # already present keeps an upgrade from re-running `apt-get update`, whose index
        # refresh can fail (e.g. a third-party repo's clock skew) and would otherwise
        # abort the whole install under `set -e` before the claurst download.
        "ldconfig -p 2>/dev/null | grep -q libasound.so.2 || { "
        "sudo -E apt-get update -qq; "
        "sudo -E apt-get install -y -qq libasound2t64 "
        "|| sudo -E apt-get install -y -qq libasound2; }; "
        'd=$(mktemp -d); cd "$d"; '
        # Resume + retry: the egress proxy truncates the CDN transfer intermittently.
        "curl -fsSL --retry 5 --retry-all-errors --connect-timeout 30 -C - "
        f"-o claurst.tgz {CLAURST_RELEASE_URL}; "
        "tar xzf claurst.tgz; mkdir -p ~/.local/bin; "
        'install -m 0755 "$(find . -name claurst -type f | head -1)" ~/.local/bin/claurst; '
        # Stamp the danno pin so the next launch's skip recognises this exact fork build.
        f'mkdir -p "$(dirname "$stamp")"; printf %s "{CLAURST_VERSION}" > "$stamp"; '
        "claurst --version"
    )
    return exec_in_container(
        runner, sandbox, script, why=f"install claurst v{CLAURST_VERSION} in sandbox '{sandbox}'"
    )


def interactive_launch_script(
    model_ref: str | None, passthru: list[str], *, capture_port: int | None = None
) -> list[str]:
    """`container_argv` for an INTERACTIVE claurst session — the `danno sandbox start
    --agent claurst` counterpart of headless `claurst_run`.

    Returns `["bash", "-lc", <script>]` where the script is the SAME Ollama-relay
    bracket the headless path uses (`driver._claurst_script`: relay backgrounded on
    127.0.0.1:11434, reaped via `trap … EXIT`) wrapped around a TTY claurst run — no
    `-p`, so claurst opens its interactive UI. The relay lives exactly as long as this
    single long-running `docker sandbox exec` (the whole session), which is why no
    persistent daemon is needed; the headless per-turn path is reused unchanged.

    `model_ref` is claurst's `-m <provider>/<tag>` (already resolved + checked by the
    caller); `passthru` is the agent's `--`-forwarded args, verbatim. A LOCAL Ollama ref
    (`ollama/…`, or None for claurst's default) is run inside the relay bracket as above.
    A CLOUD ref (e.g. `nvidia/…`) needs no relay: claurst dials the provider directly
    through the sandbox egress proxy (`HTTPS_PROXY` is in the env-file, and the fork build
    honors it), with the provider key injected by `start` — so the command is a plain
    `claurst` argv, no `OLLAMA_HOST`. `capture_port`, when set (`--capture`), points the
    relay at a host-side recording proxy so claurst's Ollama wire traffic is recorded
    (buffered, so live token-streaming is lost); it applies only to the local relay path,
    not cloud."""
    argv = ["claurst"]
    if model_ref is not None:
        argv += [CLAURST_MODEL_FLAG, model_ref]
    argv += passthru
    is_local = model_ref is None or model_ref.startswith("ollama/")
    if not is_local:
        # Cloud: no Ollama relay; claurst reaches the provider via HTTPS_PROXY directly.
        return argv
    claurst_cmd = f"OLLAMA_HOST={CLAURST_OLLAMA_HOST} {shlex.join(argv)}"
    upstream_port = CLAURST_RELAY_DEFAULT_UPSTREAM_PORT if capture_port is None else capture_port
    return ["bash", "-lc", _claurst_script(claurst_cmd, upstream_port=upstream_port)]


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
