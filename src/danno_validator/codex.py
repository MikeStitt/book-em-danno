"""Codex harness-under-test: install it into a sandbox and drive the sweep with it.

Codex (OpenAI's `@openai/codex` CLI, a Rust binary shipped via npm) is NOT one of the
prebuilt `docker sandbox` images, so the validator hosts it in a `shell` sandbox and
installs it post-provision with `npm install -g @openai/codex` (proxy-aware; the `shell`
image already carries node/npm — Phase-0 spike, 2026-07-18). It speaks ONLY the OpenAI
Responses API (`/v1/responses`) and is RELAY-FREE like claurst: the Rust client honors the
sandbox egress proxy, so it dials host Ollama's experimental Responses endpoint at
`host.docker.internal:11434/v1` directly (no in-VM relay). The sweep then drives codex via
`driver.codex_run`, which writes codex's per-turn `config.toml` inline (bench can't seed a
VM file pre-provision) and runs `codex exec --json`; this module supplies only the install
step and the `TurnFn` seam, so `run_sweep`/`run_tiers`/the oracle stay unchanged.

Scope (Phase-0): codex runs **local Ollama** (Ollama ≥ 0.13.3 for `/v1/responses`). A cloud
codex row over the Responses API is not yet spiked — `config.codex_provider_id` fails loud
for a non-Ollama backend rather than launching to a silent mid-session failure. Full
findings + the argv/config/event-schema pins: `.docs/codex-integration.md`.
"""

from __future__ import annotations

from pathlib import Path

from book_em_danno.commands.sandbox import exec_in_container
from book_em_danno.core.exec import Runner
from danno_validator.driver import (
    CODEX_HOME_DIR,
    Turn,
    TurnFn,
    _codex_base_url,
    codex_run,
)

# The prebuilt sandbox image that hosts codex. `shell` carries the toolchain codex needs
# (node/npm to install, git, coreutils) — verified in the Phase-0 spike.
CODEX_SANDBOX_IMAGE = "shell"

# Pinned codex-cli release (the npm dist-tag danno installs). Bump deliberately (and re-pin
# the NDJSON event schema in driver.py against the new version — the M1 discipline). Unlike
# claurst (a curl-fetched danno fork), codex is an upstream npm package, so `--version`
# reports exactly this and the install skip can gate on it directly.
CODEX_VERSION = "0.144.5"
CODEX_NPM_PKG = "@openai/codex"


def install_codex(runner: Runner, sandbox: str) -> list[str]:
    """Install the codex CLI into `sandbox` (`npm install -g`, idempotent).

    Skips the work when the pinned version is already on PATH (`codex --version` equals
    `CODEX_VERSION`), so a `--keep-sandboxes` re-run is cheap while a version bump forces a
    reinstall. npm is proxy-aware in the `shell` VM, so the global install reaches the
    registry through the egress proxy (verified Phase-0). Fails loud (CommandFailedError via
    `exec_in_container` under --apply) if the install fails. Returns the exec command for
    inspection."""
    script = (
        # Skip only if codex is present AND reports exactly the pinned version (codex is an
        # upstream npm package, so unlike claurst its `--version` is a reliable gate).
        "command -v codex >/dev/null 2>&1 "
        f"&& [ \"$(codex --version 2>/dev/null | grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' | head -1)\" "
        f'= "{CODEX_VERSION}" ] '
        "&& { codex --version; exit 0; }; "
        "set -e; "
        f"npm install -g {CODEX_NPM_PKG}@{CODEX_VERSION}; "
        "codex --version"
    )
    return exec_in_container(
        runner, sandbox, script, why=f"install codex v{CODEX_VERSION} in sandbox '{sandbox}'"
    )


def interactive_launch_script(
    model_ref: str | None, passthru: list[str], *, capture_port: int | None = None
) -> list[str]:
    """`container_argv` for an INTERACTIVE codex session — the `danno sandbox start --agent
    codex` counterpart of headless `codex_run`.

    Returns `["bash", "-lc", <script>]` that writes codex's `config.toml` into a VM-local
    CODEX_HOME (custom `ollama-danno` provider → host Ollama's `/v1` Responses endpoint,
    relay-free through the egress proxy — or the `--capture` recording proxy) and then
    launches an interactive `codex` (no `exec`, so it opens its TUI). `model_ref` is the bare
    model tag (already resolved by the caller) passed as `-m`; `passthru` is the agent's
    `--`-forwarded args, verbatim. Config generation for interactive parity also lives in
    `generate.generate_codex_config`; this inline form keeps the launch self-contained (no
    dependency on a pre-emitted home file, matching the headless path)."""
    import shlex

    from book_em_danno.config.generate import codex_config_toml

    base_url = _codex_base_url(capture_port)
    config_toml = codex_config_toml(base_url)
    argv = ["codex"]
    if model_ref is not None:
        argv += ["-m", model_ref]
    argv += passthru
    script = (
        f'set -e; export CODEX_HOME={CODEX_HOME_DIR}; mkdir -p "$CODEX_HOME"; '
        f"cat > \"$CODEX_HOME/config.toml\" <<'DANNO_CODEX_EOF'\n"
        f"{config_toml}\n"
        f"DANNO_CODEX_EOF\n"
        f"{shlex.join(argv)}"
    )
    return ["bash", "-lc", script]


def authed_codex_run(
    env_file: Path | None,
    capture_port: int | None = None,
    model_override: str | None = None,
    max_turns: int | None = None,
) -> TurnFn:
    """A `TurnFn` that drives `codex_run` with `env_file` bound, for `run_sweep`/bench.

    Mirrors `claurst.authed_claurst_run` so the level runners just call a plain `TurnFn`.
    Local Ollama codex needs no auth; `env_file` is forwarded to the exec for matrix parity
    and is harmless when None. codex's per-turn config.toml is written inside `codex_run`;
    `capture_port` (from `--capture`) points its Responses base_url at the recording proxy.

    `model_override`, when set, is the bare tag codex actually dials (`-m`) instead of the
    caller's generic matrix ref — bench/sweep report the generic `ollama/<tag>` ref (to keep
    the grid + headroom lookups keyed consistently) but codex's `-m` takes the bare tag
    (`Harness.dial_ref` strips the prefix). `max_turns` has no codex flag (codex `exec` has
    no polite-stop cap — the external watchdog is the bound) and is ignored."""

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
        return codex_run(
            runner,
            name,
            prompt,
            session=session,
            agent=agent,
            model=model_override if model_override is not None else model,
            skip_permissions=skip_permissions,
            workspace=workspace,
            env_file=env_file,
            capture_port=capture_port,
            max_turns=max_turns,
        )

    return run
