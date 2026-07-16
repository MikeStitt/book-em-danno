"""occ (open-claude-code) harness-under-test: install it into a sandbox and drive it.

occ is danno's fork of open-claude-code (`MikeStitt/open-claude-code`, branch
`danno-integration`, pinned by `OCC_REF`) — a Node/ESM Claude-Code clone. It is NOT a
prebuilt `docker sandbox` image, so — like claurst — the validator hosts it in a `shell`
sandbox and provisions it post-create. UNLIKE claurst (a release binary), occ is
git-cloned from source and run interpreted (`node <clone>/v2/src/index.mjs`). The fork
carries natively (see its ADR-004) what used to be host-side hacks: `detectProvider`
routes to the OpenAI-compatible path whenever `OPENAI_BASE_URL` is set, and a global
undici dispatcher (installed at startup) both raises the request timeout
(`CLAUDE_CODE_API_TIMEOUT`) and honors `HTTPS_PROXY` — so danno no longer patches source
or injects a `NODE_OPTIONS` shim. The clone is pinned by `OCC_REPO` + `OCC_REF` (ref =
commit SHA | tag | branch), resolvable per-project via the unified `[env]` mechanism.

Local Ollama and cloud (OpenAI-compatible, e.g. NVIDIA NIM) both work:
- **local** reaches host Ollama through the same plain-forward relay claurst uses (the
  sandbox's squid proxy rejects `CONNECT` to :11434, so Node's undici can't tunnel it);
- **cloud** dials the provider at :443 through the proxy; the fork's dispatcher reads
  `HTTPS_PROXY` from the injected env-file, so no shim is needed.

Two non-obvious requirements the integration spike pinned (both handled here):
`CLAUDE_CODE_STREAMING=0` (occ's OpenAI path is non-streaming; the default crashes) and a
dummy `OPENAI_API_KEY` on the local path (occ requires the Bearer header; Ollama ignores
it). Tunable ceilings (`CLAUDE_CODE_API_TIMEOUT`, `CLAUDE_CODE_MAX_RECURSION_DEPTH`,
`CLAUDE_CODE_MAX_TURNS`) ride the env-file as level-4 `harness_env("occ")` defaults, so
`danno.toml [env]` can override them. See `driver.occ_run` for the drive seam; this module
supplies install + the launch/`TurnFn` seams so `run_sweep`/the oracle stay unchanged.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from book_em_danno.commands.sandbox import exec_in_container
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import CommandFailedError, Runner
from danno_validator.driver import (
    CLAURST_RELAY_DEFAULT_UPSTREAM_PORT,
    OCC_ENTRY,
    OCC_LOCAL_OPENAI_ENV,
    OCC_MODEL_FLAG,
    OCC_PERMISSION_FLAG,
    OCC_PERMISSION_VALUE,
    OCC_STREAMING_ENV,
    Turn,
    TurnFn,
    _claurst_script,
    occ_model_target,
    occ_run,
)

# The prebuilt sandbox image that hosts occ. `shell` carries the toolchain occ needs:
# node + npm (interpreted runtime + `npm install undici`), git (clone the source),
# python3 (the relay), curl, tar — the same image claurst rides.
OCC_SANDBOX_IMAGE = "shell"

# The clone target + version stamp inside the VM. FIXED absolute paths (not $HOME-relative)
# so a relocated HOME (agent-home) can't move the entrypoint out from under the driver —
# occ's code stays VM-local exactly like claurst's binary. `OCC_ENTRY` (driver.py) lives
# under `OCC_CLONE_DIR` so the paths cannot drift.
OCC_CLONE_DIR = "/home/agent/.local/share/danno/occ"
OCC_VERSION_STAMP = "/home/agent/.local/share/danno/occ-version"

# Default source pin. Points at danno's fork (`MikeStitt/open-claude-code`, branch
# `danno-integration`), which carries the native, env-configurable knobs that used to be
# host-side hacks (detectProvider routing on OPENAI_BASE_URL, the global undici dispatcher
# for CLAUDE_CODE_API_TIMEOUT, env-driven recursion/turn caps — see the fork's ADR-004).
# `OCC_REF` is pinned to a specific commit (NOT the branch tip): occ's stream-json schema
# (parsed in `driver.OccTurn`) can drift across commits, silently zeroing tool/text
# signals — re-verify the OccTurn mapping when bumping this SHA. Overridable per-project via
# [env] or an exported `OCC_REPO`/`OCC_REF` (e.g. to track upstream `ruvnet/open-claude-code`).
OCC_REPO_DEFAULT = "https://github.com/MikeStitt/open-claude-code"
OCC_REF_DEFAULT = "d0e5c6c2a02754d9be41ed4e330f93f02afbd83b"


def occ_repo_ref(config: DannoConfig | None) -> tuple[str, str]:
    """Resolve the `(repo, ref)` occ is installed from, with precedence (highest wins):

        exported host env (`OCC_REPO`/`OCC_REF`)  >  danno.toml `[env]`  >  code default

    This is the user-requested "set the version first through ENV variables, then
    danno.toml" flow. It is a purpose-built resolver rather than `assemble_harness_env`:
    the pins are install-time code defaults, and the general assembler deliberately does
    NOT let a bare host var override a code default (that guard protects `OLLAMA_BASE_URL`
    etc.), whereas here `export OCC_REF=…` MUST win. A `{env:VAR}` indirection in an
    `[env]` value is honored; an unset indirection fails loud (Working Rule 8)."""

    def resolve(key: str, default: str) -> str:
        host = os.environ.get(key)
        if host:
            return host
        if config is not None and key in config.env:
            raw = config.env[key]
            from book_em_danno.commands.sandbox import _resolve_env_indirection

            value, missing = _resolve_env_indirection(raw)
            if missing:
                raise CommandFailedError(
                    f"danno.toml [env] {key} references unset host var(s) {', '.join(missing)}. "
                    f"Export them or set {key} directly."
                )
            return value
        return default

    return resolve("OCC_REPO", OCC_REPO_DEFAULT), resolve("OCC_REF", OCC_REF_DEFAULT)


def install_occ(runner: Runner, sandbox: str, config: DannoConfig | None = None) -> list[str]:
    """Clone occ into `sandbox` (git-cloned, idempotent).

    Steps (mirrors `claurst.install_claurst`, but from source): skip-guard on a danno
    stamp (`repo@ref`) + entrypoint present; full `git clone` (NOT --depth 1 — `OCC_REF`
    may be an arbitrary commit) then `git checkout <ref>`; `npm install` in the clone (the
    fork declares `undici` as a dependency, needed for its global fetch dispatcher — see
    ADR-004); stamp `repo@ref`. git + npm are proxy-aware (they clone/install through the
    squid egress proxy). No source patching and no shim are written: the fork carries the
    detectProvider routing and the undici dispatcher natively. Fails loud
    (CommandFailedError via `exec_in_container` under --apply) if any step fails. Returns
    the exec command for inspection."""
    repo, ref = occ_repo_ref(config)
    entry = OCC_ENTRY
    stamp_val = f"{repo}@{ref}"
    lines = [
        # Skip only when the danno stamp matches this exact repo@ref AND the entrypoint is
        # present — a different pin (or a half-clone) falls through to a clean reinstall.
        f'stamp="{OCC_VERSION_STAMP}"; '
        f'if [ "$(cat "$stamp" 2>/dev/null)" = "{stamp_val}" ] && [ -f "{entry}" ]; then '
        f'echo "occ {stamp_val} already installed"; exit 0; fi',
        "set -e",
        # occ is interpreted; node + npm are mandatory. Fail loud if the image lacks them.
        "if ! command -v node >/dev/null 2>&1; then "
        'echo "occ needs node in the sandbox, but it is not on PATH" >&2; exit 1; fi',
        "if ! command -v npm >/dev/null 2>&1; then "
        'echo "occ needs npm in the sandbox, but it is not on PATH" >&2; exit 1; fi',
        # Clean clone + pin. Full clone (no --depth) so an arbitrary commit ref checks out.
        f'rm -rf "{OCC_CLONE_DIR}"; mkdir -p "$(dirname "{OCC_CLONE_DIR}")"',
        f'git clone "{repo}" "{OCC_CLONE_DIR}"',
        f'git -C "{OCC_CLONE_DIR}" checkout "{ref}"',
        # Install the fork's declared deps (undici for the global dispatcher). Prefer the
        # v2/ workspace (where package.json lives); the explicit undici install is a
        # belt-and-suspenders guarantee independent of the lockfile.
        f'npm --prefix "{OCC_CLONE_DIR}/v2" install >/dev/null 2>&1 || '
        f'(cd "{OCC_CLONE_DIR}/v2" && npm install)',
        f'npm --prefix "{OCC_CLONE_DIR}/v2" install undici >/dev/null 2>&1 || '
        f'(cd "{OCC_CLONE_DIR}/v2" && npm install undici)',
        # Stamp the pin so the next launch's skip recognises this exact repo@ref.
        f'mkdir -p "$(dirname "$stamp")"; printf %s "{stamp_val}" > "$stamp"',
        f'echo "occ {stamp_val} installed"',
    ]
    script = "\n".join(lines)
    return exec_in_container(
        runner, sandbox, script, why=f"install occ ({stamp_val}) in sandbox '{sandbox}'"
    )


def interactive_launch_script(
    model_ref: str | None, passthru: list[str], *, capture_port: int | None = None
) -> list[str]:
    """`container_argv` for an INTERACTIVE occ session — the `danno sandbox start --agent
    occ` counterpart of headless `occ_run`.

    Returns `["bash", "-lc", <script>]`. occ opens its Ink TUI when given no `-p`. A LOCAL
    Ollama ref runs inside the SAME relay bracket the headless path uses
    (`driver._claurst_script`), with occ's OpenAI env (`OPENAI_BASE_URL`→the relay + dummy
    key) + `CLAUDE_CODE_STREAMING=0` set inline. A CLOUD ref needs no relay: the fork's
    global undici dispatcher reads `HTTPS_PROXY` from the session env-file (injected by
    `start`, see `sandbox.occ_cloud_env_lines`, which also carries the provider's
    `OPENAI_BASE_URL` + `OPENAI_API_KEY`) — so no shim is needed, only
    `CLAUDE_CODE_STREAMING=0` inline. `CLAUDE_CODE_STREAMING=0` disables live token
    streaming (occ's OpenAI path is non-streaming), so the TUI renders each response at
    once; `capture_port` (`--capture`) applies only to the local relay path."""
    m_value, is_local = occ_model_target(model_ref)
    node_argv = ["node", OCC_ENTRY, OCC_PERMISSION_FLAG, OCC_PERMISSION_VALUE]
    if m_value is not None:
        node_argv += [OCC_MODEL_FLAG, m_value]
    node_argv += passthru
    if not is_local:
        occ_cmd = f"{OCC_STREAMING_ENV} {shlex.join(node_argv)}"
        return ["bash", "-lc", occ_cmd]
    occ_cmd = f"{OCC_LOCAL_OPENAI_ENV} {OCC_STREAMING_ENV} {shlex.join(node_argv)}"
    upstream_port = CLAURST_RELAY_DEFAULT_UPSTREAM_PORT if capture_port is None else capture_port
    return ["bash", "-lc", _claurst_script(occ_cmd, upstream_port=upstream_port)]


def authed_occ_run(
    env_file: Path | None,
    capture_port: int | None = None,
    model_override: str | None = None,
    max_turns: int | None = None,
) -> TurnFn:
    """A `TurnFn` that drives `occ_run` with `env_file` bound, for `run_sweep`.

    Mirrors `claurst.authed_claurst_run`. Local Ollama occ needs no auth; `env_file` is
    forwarded to the exec for matrix parity (cloud configs, which carry OPENAI_BASE_URL +
    OPENAI_API_KEY) and is harmless when None. The relay is set up inside `occ_run` per turn.
    `capture_port` (from `--capture`) points that relay at the recording proxy.

    `model_override`, when set, is the ref occ actually dials (`-m`) instead of the caller's
    generic matrix ref. Bench/sweep report the generic `<backend>/<tag>` ref (to keep the
    comparison grid + headroom lookups keyed consistently) but must dial a ref whose leading
    segment `occ_run`'s locality check understands — an Ollama backend named anything other
    than the literal `ollama` would otherwise be misread as cloud and fall back to Anthropic.
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
        return occ_run(
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
