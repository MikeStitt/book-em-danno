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

import hashlib
import json
import os
import re
import subprocess
import tempfile
import tomllib
import urllib.parse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from ..capture.wiring import (
    CaptureTarget,
    capture_allow_hosts,
    captures_running,
    plan_capture,
    uncaptured_cloud_refs,
)
from ..config.generate import (
    _CLAURST_MODELS_FILE,
    Action,
    GenerateResult,
    claurst_model_ref,
    generate,
    generate_claurst,
)
from ..config.loader import DannoConfigError, load_config
from ..config.schema import DannoConfig, NpmPlugin, OllamaBackend, OpenAIBackend, Sandbox
from ..core import registry
from ..core.exec import CommandFailedError, Runner, log_info, log_warn
from . import ollama, sandbox_cli

DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434/v1"
DEFAULT_ALLOW_HOSTS = ("localhost:11434",)
DEFAULT_HARNESS = "opencode"
# Auth env vars Claude Code accepts, in preference order (subscription token first).
CLAUDE_AUTH_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
# claurst (a pure-Rust Claude-Code clone) is NOT a prebuilt `docker sandbox` image: it
# is hosted in the `shell` image and the release binary is installed post-create (see
# `danno_validator.claurst.install_claurst`). The logical harness label stays "claurst"
# everywhere (naming/registry/env/launch); only the create-time Docker image differs.
CLAURST_HARNESS = "claurst"
# claurst's config dir inside its HOME (`~/.claurst`); danno writes its generated
# registry overlay + settings.json here (see `_emit_claurst_config`). The models
# filename is shared with the generator so the two cannot drift.
_CLAURST_DIR = ".claurst"
# occ (open-claude-code, a Node/ESM Claude-Code clone) is likewise NOT a prebuilt
# `docker sandbox` image: it is git-cloned into the `shell` image post-create (see
# `danno_validator.occ.install_occ`). Like claurst, the logical label stays "occ"
# everywhere; only the create-time Docker image differs.
OCC_HARNESS = "occ"
# Generous level-4 agentic-loop ceilings for occ against slow local models. The fork reads
# these from the environment (see its ADR-004); danno supplies them as env-file DEFAULTS so
# `danno.toml [env]` (or an exported host var) can override them via `assemble_harness_env`.
# 60-min API timeout matches the relay's DANNO_RELAY_TIMEOUT default; the deep tool-recursion
# cap keeps long local loops from hitting occ's faithful default of 50. Max-turns is left to
# occ's `--max-turns` flag (driver.OCC_DEFAULT_MAX_TURNS), so it is not defaulted here.
OCC_API_TIMEOUT_DEFAULT_MS = 3600000
OCC_MAX_RECURSION_DEPTH_DEFAULT = 500


def _docker_image(harness: str) -> str:
    """The prebuilt `docker sandbox` image backing a logical harness label.

    Almost always the label itself (opencode/claude/… ARE images). claurst and occ are
    the exceptions — neither has a prebuilt image, so both ride the `shell` image and are
    installed afterwards; the label is preserved for the sandbox name and registry."""
    if harness == CLAURST_HARNESS:
        from danno_validator.claurst import CLAURST_SANDBOX_IMAGE  # local: avoids import cycle

        return CLAURST_SANDBOX_IMAGE
    if harness == OCC_HARNESS:
        from danno_validator.occ import OCC_SANDBOX_IMAGE  # local: avoids import cycle

        return OCC_SANDBOX_IMAGE
    return harness


def default_name(target_abs: Path, harness: str = DEFAULT_HARNESS) -> str:
    """Sandbox name derived from the project's *parent and own* dir names.

    `danno-<parent>-<base>` so same-basename projects in different parents
    (`~/work/acme` vs `~/clients/acme`) and worktree dirs (`…/main`, `…/login`)
    stay distinct. The default opencode harness keeps the bare name; a per-harness
    suffix otherwise so harnesses get separate sandboxes that can coexist.
    """
    parent = target_abs.parent.name
    stem = f"{parent}-{target_abs.name}" if parent else target_abs.name
    base = f"danno-{stem}"
    return base if harness == DEFAULT_HARNESS else f"{base}-{harness}"


def live_sandbox_names() -> set[str]:
    """The set of sandbox names the active CLI reports (empty if unavailable).

    `sbx ls -q` emits one bare name per line (empty when none); legacy
    `docker sandbox ls` is a header + table (skip row 0, first column)."""
    argv, quiet = sandbox_cli.ls_names_argv()
    try:
        out = subprocess.run(argv, capture_output=True, text=True, check=False).stdout
    except (FileNotFoundError, OSError):
        return set()
    if quiet:
        return {line.strip() for line in out.splitlines() if line.strip()}
    return {line.split()[0] for line in out.splitlines()[1:] if line.split()}


def sandbox_exists(name: str) -> bool:
    """True if a sandbox named `name` is listed by the active CLI's `ls`."""
    return name in live_sandbox_names()


def _sbx_policy_initialized() -> bool:
    """True if the sbx global network policy is initialized (`sbx policy ls`
    succeeds). Treated as True/absent for docker (no such requirement)."""
    try:
        return (
            subprocess.run(
                sandbox_cli.policy_ls_argv(), capture_output=True, text=True, check=False
            ).returncode
            == 0
        )
    except (FileNotFoundError, OSError):
        return False


def ensure_policy_initialized(runner: Runner) -> None:
    """sbx requires a one-time global network-policy init before `create`; the
    legacy `docker sandbox` has no such step. If the active backend is sbx and the
    policy is not yet initialized, advise `sbx policy init balanced` (under --apply
    it runs). No-op for docker and when already initialized (init is not
    idempotent, so we gate on `sbx policy ls`)."""
    init_argv = sandbox_cli.policy_init_argv()
    if init_argv is None or _sbx_policy_initialized():
        return
    runner.advise(init_argv, why="initialize the sbx global network policy (one-time)")


def _live(runner: Runner) -> bool:
    """True when we are actually executing (so existence checks are meaningful)."""
    return runner.apply


def _agent_home_root() -> Path:
    """Root of all danno-managed agent-home folders on the host."""
    return Path.home() / ".danno" / "agent-home"


def _git_common_dir(target_abs: Path) -> Path:
    """The repo's shared `.git` common dir (worktrees share one). Fails loud if
    `target_abs` is not in a git work tree — `per-repo` has no key without it."""
    out = subprocess.run(
        ["git", "-C", str(target_abs), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise CommandFailedError(
            f"agent_home = 'per-repo' but {target_abs} is not in a git work tree "
            f"({out.stderr.strip() or 'git rev-parse failed'}). Use 'per-project' or a path."
        )
    return Path(out.stdout.strip())


def resolve_agent_home(
    value: str, target_abs: Path, sandbox_name: str, *, relative_base: Path | None = None
) -> Path | None:
    """Map an `agent_home` identity key to a host directory (None = ephemeral).

    `relative_base` is the directory a *relative* explicit path resolves against
    (the `danno.workspace.toml` dir when inherited); it defaults to `target_abs`.
    """
    root = _agent_home_root()
    if value == "ephemeral":
        return None
    if value == "per-project":
        return root / sandbox_name
    if value == "shared":
        return root / "shared"
    if value == "per-repo":
        common = _git_common_dir(target_abs)
        repo_base = common.parent.name if common.name == ".git" else common.name
        digest = hashlib.sha256(str(common).encode()).hexdigest()[:6]
        return root / "repos" / f"{repo_base}-{digest}"
    if value.startswith("group:"):
        return root / "groups" / value[len("group:") :]
    # Explicit path: ~ expands; a relative path resolves against relative_base.
    home = Path(value).expanduser()
    if not home.is_absolute():
        home = (relative_base or target_abs) / home
    return home.resolve()


def _read_sandbox(toml_path: Path) -> Sandbox | None:
    """Parse just the `[sandbox]` block from a danno.toml / danno.workspace.toml.

    None if the file or its `[sandbox]` section is absent; raises
    `DannoConfigError` (fail loud, Working Rule 8) on bad TOML or an invalid
    `[sandbox]`.
    """
    if not toml_path.is_file():
        return None
    try:
        raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise DannoConfigError(f"invalid TOML in {toml_path}: {exc}") from exc
    if "sandbox" not in raw:
        return None
    try:
        return Sandbox.model_validate(raw["sandbox"])
    except ValidationError as exc:
        raise DannoConfigError(f"invalid [sandbox] in {toml_path}:\n{exc}") from exc


def _find_workspace(target_abs: Path) -> tuple[Path | None, Sandbox | None]:
    """Walk up to the nearest `danno.workspace.toml`; return its dir and `[sandbox]`."""
    for d in [target_abs, *target_abs.parents]:
        if (d / "danno.workspace.toml").is_file():
            return d, _read_sandbox(d / "danno.workspace.toml")
    return None, None


def _find_config_ancestor(target_abs: Path) -> Path | None:
    """Nearest *ancestor* dir carrying a danno.toml or danno.workspace.toml."""
    for d in target_abs.parents:
        if (d / "danno.toml").is_file() or (d / "danno.workspace.toml").is_file():
            return d
    return None


def resolve_home(target_abs: Path, sandbox_name: str) -> Path | None:
    """The agent-home dir for `target_abs` (None = ephemeral, the VM-local default).

    Reads `<target>/danno.toml [sandbox]`; if that file has no `[sandbox]`, walks
    up to the nearest `danno.workspace.toml` and inherits its `[sandbox]` (a
    relative `agent_home` then resolves against the workspace dir). Emits the
    misplaced-cwd hint and the in-repo footgun warning. Raises `DannoConfigError`
    on a malformed config.
    """
    own = _read_sandbox(target_abs / "danno.toml")
    workspace_dir, workspace_sb = _find_workspace(target_abs)

    value: str
    relative_base: Path
    if own is not None:
        value, relative_base = own.agent_home, target_abs
    elif workspace_sb is not None:
        assert workspace_dir is not None  # paired with workspace_sb by _find_workspace
        value, relative_base = workspace_sb.agent_home, workspace_dir
    else:
        value, relative_base = Sandbox().agent_home, target_abs

    # Misplaced-cwd hint: a config-presence signal, not .git geometry. Fires only
    # when this dir carries no config and isn't governed by a workspace file, yet
    # an ancestor does — i.e. you're probably standing in the wrong directory.
    if not (target_abs / "danno.toml").is_file() and workspace_dir is None:
        ancestor = _find_config_ancestor(target_abs)
        if ancestor is not None:
            log_warn(
                f"{target_abs} has no danno.toml; project root looks like {ancestor} — "
                "run there or pass --target. Proceeding with this directory."
            )

    home = resolve_agent_home(value, target_abs, sandbox_name, relative_base=relative_base)

    if home is not None and home.is_relative_to(target_abs):
        log_warn(
            f"harness home {home} is inside the repo {target_abs} — chat history would "
            f"land in your project (and could be committed). Add it to .gitignore or "
            "point agent_home at a path outside the repo."
        )
    return home


def seed_onboarding(home: Path, workspace: Path) -> None:
    """Pre-mark Claude onboarding AND per-workspace trust so neither the theme/login
    wizard (which can mask a valid env auth token on a fresh VM — Working Rule 8
    trap) nor the "trust this folder" dialog blocks a launch. Merges into
    `<home>/.claude.json` without clobbering existing keys; idempotent.

    `workspace` is the in-container repo path; `docker sandbox` mounts it at the same
    absolute path as on the host, so it matches the key Claude stores trust under
    (`projects.<path>.hasTrustDialogAccepted`)."""
    home.mkdir(parents=True, exist_ok=True)
    f = home / ".claude.json"
    data: dict[str, object] = {}
    if f.is_file():
        try:
            loaded = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except json.JSONDecodeError:
            data = {}
    data.setdefault("hasCompletedOnboarding", True)
    data.setdefault("theme", "dark")
    projects = data.setdefault("projects", {})
    if isinstance(projects, dict):
        proj = projects.setdefault(str(workspace), {})
        if isinstance(proj, dict):
            proj.setdefault("hasTrustDialogAccepted", True)
            proj.setdefault("hasCompletedProjectOnboarding", True)
    f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def create(
    runner: Runner,
    name: str,
    target_abs: Path,
    harness: str = DEFAULT_HARNESS,
    *,
    home: Path | None = None,
    registry_path: Path | None = None,
) -> list[str]:
    """Create the sandbox, mounting the project at the same path inside the VM.

    `harness` selects the Docker prebuilt harness baked into the VM (opencode, claude,
    …). When `home` is set, the host agent-home dir is mounted as a second
    workspace (and `mkdir -p`'d first so the mount has a source). With
    `registry_path`, the name→target mapping is recorded under --apply (advised
    otherwise) and a loud warning fires if `name` already maps elsewhere.
    Idempotent under --apply: an already-existing sandbox is left in place.
    """
    ensure_policy_initialized(runner)  # sbx needs a one-time global policy init before create

    if registry_path is not None:
        existing = registry.lookup(registry_path, name)
        if existing is not None and existing.get("target") != str(target_abs):
            log_warn(
                f"sandbox name '{name}' already maps to {existing['target']}; creating it "
                f"for {target_abs} would collide — pass --name to disambiguate."
            )

    cmd = [*sandbox_cli.base(), "create", "--name", name, _docker_image(harness), str(target_abs)]
    if home is not None:
        cmd.append(str(home))

    if _live(runner) and sandbox_exists(name):
        log_info(f"sandbox '{name}' already exists — skipping create")
    else:
        if home is not None:
            runner.advise(["mkdir", "-p", str(home)], why=f"ensure harness home {home} exists")
        runner.advise(cmd, why=f"create the {harness} sandbox '{name}' for {target_abs}")

    if registry_path is not None:
        if _live(runner):
            registry.record(registry_path, name, str(target_abs), harness)
        else:
            log_info(f"would record '{name}' → {target_abs} in {registry_path}")
    return cmd


def _ollama_hostport(url: str) -> str:
    """The `host:port` the sandbox dials for Ollama, parsed from a base URL."""
    return urllib.parse.urlparse(url).netloc or url


# SBX-WORKAROUND(OpenShell#263): local-host Ollama aliases. sbx has no
# host.docker.internal→localhost rewrite (docker sandbox did) and reaches a SAME-HOST
# Ollama via 127.0.0.1 forced through its host-side proxy. Verified 2026-07-09: with
# `127.0.0.1:11434` allowed, a request routed through the proxy returns 200 (the proxy
# is host-side, so its loopback is the host's); an unallowed rule/port returns 403.
# So for sbx these aliases resolve to loopback (network-independent: no LAN IP, no VPN,
# works offline); a concrete/remote host is used literally. NOTE: the harness must
# ROUTE 127.0.0.1 through the proxy (drop it from NO_PROXY) — the Phase-2 harness-env
# wiring; claurst keeps its own in-sandbox 127.0.0.1 relay instead. Remove this whole
# resolver once sbx routes host.docker.internal.
_LOCAL_OLLAMA_ALIASES = (
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "host.docker.internal",
    "gateway.docker.internal",
)
_SBX_LOOPBACK = "127.0.0.1"


def resolve_ollama_hostport(hostport: str, *, resolve: bool) -> tuple[str, str | None]:
    """Resolve an Ollama `host:port` for the sbx egress allow-rule (and the harness
    URL, kept consistent). A LOCAL alias resolves to `127.0.0.1:<port>` — an sbx
    sandbox reaches a same-host Ollama via loopback forced through the host proxy,
    which is network-independent (no LAN IP, no VPN, works offline). A concrete
    IP/hostname (a remote Ollama) is returned unchanged. Returns `(hostport, warning)`;
    a non-None warning is a fail-loud message. SBX-WORKAROUND(OpenShell#263)."""
    host, sep, port = hostport.rpartition(":")
    if not sep:
        host, port = hostport, "11434"
    host = host.strip("[]")  # tolerate bracketed IPv6
    if host not in _LOCAL_OLLAMA_ALIASES:
        return hostport, None  # concrete / remote address -> used literally
    if not resolve:
        return hostport, (
            f"sbx: Ollama at '{host}' is a local alias. sbx reaches a same-host Ollama via "
            "127.0.0.1 through its host proxy — enable [sandbox].resolve_ollama_host, or set "
            "a concrete remote base_url."
        )
    return f"{_SBX_LOOPBACK}:{port}", None  # local -> loopback via the host proxy


def configure_proxy(
    runner: Runner,
    name: str,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
    *,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    resolve_ollama_host: bool = True,
) -> list[str]:
    """Set the egress hole: allow ONLY the Ollama endpoint; host/LAN otherwise denied.

    docker: its egress proxy rewrites `host.docker.internal`→`localhost`, so the
    default `allow_hosts` (`localhost:11434`) is correct — used as-is. sbx has no such
    rewrite; a LOCAL Ollama is reached at `127.0.0.1:port` through the host proxy, so
    `resolve_ollama_hostport` maps local aliases to loopback (SBX-WORKAROUND
    (OpenShell#263), gated by `[sandbox].resolve_ollama_host`); a concrete remote host
    is used literally. Fail-loud WARN when a local alias is left unresolved.
    """
    effective = allow_hosts
    if sandbox_cli.resolve_backend() == "sbx":
        hostport, warning = resolve_ollama_hostport(
            _ollama_hostport(ollama_url), resolve=resolve_ollama_host
        )
        if warning:
            log_warn(warning)
        else:
            log_info(f"sbx egress: allowing Ollama at {hostport}; host/LAN otherwise denied")
        # Swap the docker-proxy localhost token for the real endpoint; keep any other
        # allow_hosts (e.g. a capture proxy).
        effective = tuple(hostport if h == DEFAULT_ALLOW_HOSTS[0] else h for h in allow_hosts)
        if hostport not in effective:
            effective = (hostport, *effective)
    cmd = sandbox_cli.policy_allow_argv(name, effective)
    return runner.advise(
        cmd, why="set the egress policy (host/LAN denied except the Ollama endpoint)"
    )


def stop(runner: Runner, name: str) -> list[str]:
    """Stop the sandbox VM (also how a fresh network policy is made to take effect)."""
    return runner.advise([*sandbox_cli.base(), "stop", name], why=f"stop sandbox '{name}'")


def ensure_running(runner: Runner, name: str) -> list[str]:
    """Start a stopped-but-existing sandbox VM. `docker sandbox` has no `start`
    subcommand, but `exec … true` auto-starts the VM (then exits). Needed before
    `network proxy`, which 400s ("not running") against a stopped VM — the case hit
    when re-provisioning an existing sandbox left stopped by a prior provision."""
    return runner.advise(
        [*sandbox_cli.base(), "exec", name, "true"],
        why=f"ensure sandbox '{name}' is running before configuring its network",
    )


def provision(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    harness: str = DEFAULT_HARNESS,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
    home: Path | None = None,
    registry_path: Path | None = None,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    resolve_ollama_host: bool = True,
) -> list[list[str]]:
    """Get the sandbox to 'ready': create + egress hole + stop (so the policy applies
    on next start). Does NOT launch the TUI — that's `start`."""
    ollama.announce_loopback()
    if harness == DEFAULT_HARNESS and not (target_abs / ".opencode" / "opencode.jsonc").is_file():
        log_info(
            "[yellow]WARN[/yellow] target has no .opencode/opencode.jsonc — "
            "run `danno install` first so the sandbox has a config to load."
        )
    # A fresh `create` leaves the VM running, so `configure_proxy` works. But on a
    # re-provision `create` is skipped and the existing VM is stopped (a prior
    # provision ends with `stop`), and `network proxy` 400s against a stopped VM —
    # so bring it back up first when the sandbox already existed.
    preexisting = _live(runner) and sandbox_exists(name)
    cmds = [create(runner, name, target_abs, harness, home=home, registry_path=registry_path)]
    if preexisting:
        cmds.append(ensure_running(runner, name))
    cmds.append(
        configure_proxy(
            runner,
            name,
            allow_hosts,
            ollama_url=ollama_url,
            resolve_ollama_host=resolve_ollama_host,
        )
    )
    # The network policy only takes on a fresh VM start; stop so the next `start`
    # applies the allow-rule.
    cmds.append(stop(runner, name))
    if harness == CLAURST_HARNESS:
        # claurst has no prebuilt image: drop its binary into the `shell` VM. Done AFTER
        # the stop so the install exec auto-starts the VM with the allow-policy armed
        # (apt + the GitHub release fetch need egress). Idempotent (self-skips when
        # already installed). The binary stays VM-local; only ~/.claurst is persisted
        # via the relocated HOME at launch (see harness_env). Local import: claurst.py
        # imports back into this module.
        from danno_validator.claurst import install_claurst

        cmds.append(install_claurst(runner, name))
    if harness == OCC_HARNESS:
        # occ has no prebuilt image either: git-clone + patch it into the `shell` VM.
        # Same placement rationale as claurst (after stop → auto-start with egress armed;
        # git clone + `npm install undici` need the proxy). Idempotent (stamp skip). The
        # pins (OCC_REPO/OCC_REF) resolve from danno.toml [env] + host env, so the project
        # config is loaded here. Local import: occ.py imports back into this module.
        from danno_validator.occ import install_occ

        cmds.append(install_occ(runner, name, _maybe_load_config(target_abs)))
    return cmds


def harness_env(harness: str, ollama_url: str, home: Path | None = None) -> list[str]:
    """The harness-specific `KEY=VAL` env-file lines.

    opencode reaches host Ollama via OLLAMA_BASE_URL. claude needs auth: prefer the
    subscription token (CLAUDE_CODE_OAUTH_TOKEN), else ANTHROPIC_API_KEY, read from
    danno's host environment. Fail loud (Working Rule 8) when neither is set. The
    secret only ever lands in the chmod-600 env-file, never on the command line.

    When `home` is set, the harness's global config is relocated onto the mounted
    host dir: claude via CLAUDE_CONFIG_DIR; opencode via XDG_CONFIG_HOME; claurst via
    HOME (it reads `~/.claurst` and honors no config-dir override — verified
    `scratch/claurst_home_probe.sh`, claurst 0.1.5). The claurst binary lives VM-local
    (`/home/agent/.local/bin`, still first on PATH after the HOME swap), so only its
    config/state (`~/.claurst`) follows the relocated HOME. claurst's `~/.claurst`
    holds a SQLite `sessions.db` with no config/data split (unlike opencode, whose data
    dir stays VM-local to dodge a virtiofs WAL crash) — but a live probe confirmed a
    relocated HOME runs claurst fine AND `PRAGMA journal_mode=WAL` works on the mounted
    home here, so that crash does not reproduce and `~/.claurst` persists host-side
    (full interactive resume across stop/start is best confirmed in a real TUI session).
    opencode reaches host Ollama via OLLAMA_BASE_URL; claurst's Ollama URL is set inline
    by the relay bracket (`OLLAMA_HOST`, see `claurst.interactive_launch_script`), so no
    Ollama line here. For claurst with a home, `CLAURST_MODELS_PATH` also points at the
    danno-generated registry overlay under `{home}/.claurst` (written by
    `_emit_claurst_config`) so its tool-gating + context window come from danno.toml.
    """
    if harness == CLAURST_HARNESS:
        if home is None:
            return []
        # HOME relocation makes ~/.claurst == {home}/.claurst (mounted at the same path
        # in the VM). Point claurst at the danno-generated registry overlay there so its
        # tool-gating + context window come from danno.toml (Bug 4/7), not its catalog.
        return [f"HOME={home}", f"CLAURST_MODELS_PATH={home / _CLAURST_DIR / _CLAURST_MODELS_FILE}"]
    if harness == OCC_HARNESS:
        # occ's *mandatory* OpenAI env (OPENAI_BASE_URL/KEY, CLAUDE_CODE_STREAMING) is set
        # INLINE in the launch/headless command (like claurst's OLLAMA_HOST), NOT here, so it
        # cannot be user-overridden. Here we supply the *tunable* agentic-loop ceilings as
        # level-4 DEFAULTS, so danno.toml [env] can raise/lower them (see the fork's ADR-004).
        # The clone lives VM-local at a fixed path, so only a relocated HOME (occ's own
        # session/state) follows the mounted home; without one, occ runs entirely VM-local.
        lines = [
            f"CLAUDE_CODE_API_TIMEOUT={OCC_API_TIMEOUT_DEFAULT_MS}",
            f"CLAUDE_CODE_MAX_RECURSION_DEPTH={OCC_MAX_RECURSION_DEPTH_DEFAULT}",
        ]
        if home is not None:
            lines.append(f"HOME={home}")
        return lines
    if harness == "claude":
        lines = []
        for var in CLAUDE_AUTH_VARS:
            val = os.environ.get(var)
            if val:
                lines.append(f"{var}={val}")
                break
        else:
            raise CommandFailedError(
                "Claude Code needs auth but neither CLAUDE_CODE_OAUTH_TOKEN nor "
                "ANTHROPIC_API_KEY is set in danno's environment. Run `claude setup-token` "
                "(Max/Pro subscription) and export CLAUDE_CODE_OAUTH_TOKEN, or export "
                "ANTHROPIC_API_KEY for API billing."
            )
        if home is not None:
            lines.append(f"CLAUDE_CONFIG_DIR={home}")
        return lines
    lines = [f"OLLAMA_BASE_URL={ollama_url}"]
    if home is not None:
        lines.append(f"XDG_CONFIG_HOME={home}/config")
    return lines


def resolve_claurst_model(config: DannoConfig, value: str) -> str:
    """Resolve a `-m` value to claurst's `-m <provider>/<tag>` (ollama or a cloud provider).

    `value` is a `[models]` name (the documented form, e.g. `gemma4`) or a raw
    `provider/model` ref. Named models map through `claurst_model_ref` into claurst's own
    provider namespace: Ollama → `ollama/<tag>`, NVIDIA NIM → `nvidia/<tag>`. The fork
    build honors the sandbox egress proxy, so a cloud provider danno can fully wire (key
    injected from its `api_key_env`, reachable through the proxy) is launched; an unmapped
    cloud host still fails loud (Working Rule 8). A raw non-Ollama ref is rejected: danno
    can't derive its key env or vouch for its reachability, so it must be declared as a
    [models] entry on a supported backend instead of launching to a silent failure."""
    if "/" in value:
        provider = value.split("/", 1)[0]
        if provider != "ollama":
            raise CommandFailedError(
                f"--harness claurst: a raw '{provider}/…' ref can't be wired by danno (it can't "
                f"derive the API key or vouch for reachability). Declare it as a [models] entry "
                f"on a supported backend (Ollama, or NVIDIA NIM) and pass that name instead."
            )
        return value
    if config.models.get(value) is None:
        raise CommandFailedError(
            f"model '{value}' is not defined in danno.toml [models]. "
            f"Pass a [models] entry (Ollama or NVIDIA NIM) for --harness claurst."
        )
    try:
        return claurst_model_ref(config, value)
    except NotImplementedError as exc:
        raise CommandFailedError(
            f"--harness claurst can't reach model '{value}': {exc} Pick an Ollama model or an "
            f"NVIDIA NIM model."
        ) from exc


def claurst_cloud_key_env(config: DannoConfig, value: str) -> str | None:
    """The host env var holding the API key for a cloud claurst `-m` value, or None.

    For a [models] name on a cloud (openai) backend that's the backend's `api_key_env`
    (e.g. NVIDIA_API_KEY); `start` injects its value into the chmod-600 env-file so the
    in-VM claurst authenticates to the provider through the egress proxy. None for local
    Ollama (no key) and for raw refs (`resolve_claurst_model` only accepts raw `ollama/…`,
    which needs none)."""
    if "/" in value:
        return None
    model = config.models.get(value)
    if model is None:
        return None
    backend = config.backends[model.backend]
    if isinstance(backend, OpenAIBackend):
        return backend.api_key_env
    return None


def claurst_cloud_env_lines(config: DannoConfig, value: str) -> list[str]:
    """The env-file lines that inject a cloud claurst model's provider key, or [].

    Reads the backend's `api_key_env` from danno's host environment and emits
    `["<VAR>=<value>"]` so it lands only in the chmod-600 env-file (never a command line).
    Fails loud (Working Rule 8) when the var is unset/empty, naming it, rather than
    launching to an auth failure mid-session. Empty for local Ollama."""
    var = claurst_cloud_key_env(config, value)
    if var is None:
        return []
    val = os.environ.get(var)
    if not val:
        raise CommandFailedError(
            f"--harness claurst -m {value} needs the cloud provider key '{var}', but it is unset "
            f"in danno's environment. Export {var} (e.g. in your shell profile) and re-run."
        )
    return [f"{var}={val}"]


def cloud_api_key_env_lines(config: DannoConfig, value: str) -> list[str]:
    """Env-file lines injecting a cloud model's provider key under its OWN var name
    (`<api_key_env>=<hostval>`), or [] for a local Ollama model.

    This is the form a HUT needs when it reads the provider key under the backend's own
    env var: opencode's generated provider block references `{env:<api_key_env>}`, and
    claurst reads the same var. (occ instead needs the `OPENAI_BASE_URL`/`OPENAI_API_KEY`
    mapping — see `occ_cloud_env_lines`.) The value lands only in the chmod-600 env-file,
    never on a command line. Fails loud (Working Rule 8) when the var is unset/empty,
    naming it, rather than launching to a mid-session auth failure."""
    var = claurst_cloud_key_env(config, value)
    if var is None:
        return []
    val = os.environ.get(var)
    if not val:
        raise CommandFailedError(
            f"model '{value}' needs the cloud provider key '{var}', but it is unset in danno's "
            f"environment. Export {var} (e.g. in your shell profile) and re-run."
        )
    return [f"{var}={val}"]


def resolve_occ_model(config: DannoConfig, value: str) -> str:
    """Resolve a `-m` value to occ's `<backend>/<tag>` ref (parsed by `driver.occ_model_target`).

    `value` is a `[models]` name (e.g. `gemma4`) or a raw `ollama/<tag>` ref. An Ollama
    model always yields `ollama/<tag>` (so the driver's locality detection is by prefix,
    independent of the backend's config name); an OpenAI-compatible (cloud) model yields
    `<backend>/<tag>` — the driver strips the backend and drives occ with the bare tag as
    the provider model id, routing on `OPENAI_BASE_URL` (injected by `occ_cloud_env_lines`).
    A raw non-Ollama ref is rejected: danno can't derive its key/base URL, so it must be a
    [models] entry on a supported backend (Working Rule 8)."""
    if "/" in value:
        provider = value.split("/", 1)[0]
        if provider != "ollama":
            raise CommandFailedError(
                f"--harness occ: a raw '{provider}/…' ref can't be wired by danno (it can't "
                f"derive the OPENAI_BASE_URL/key). Declare it as a [models] entry on a "
                f"supported backend (Ollama, or an OpenAI-compatible cloud) and pass that name."
            )
        return value
    model = config.models.get(value)
    if model is None:
        raise CommandFailedError(
            f"model '{value}' is not defined in danno.toml [models]. "
            f"Pass a [models] entry (Ollama or an OpenAI-compatible cloud) for --harness occ."
        )
    backend = config.backends[model.backend]
    if isinstance(backend, OllamaBackend):
        return f"ollama/{model.tag}"
    if isinstance(backend, OpenAIBackend):
        return f"{model.backend}/{model.tag}"
    raise CommandFailedError(
        f"--harness occ can't reach model '{value}': backend kind '{backend.kind}' is not "
        f"supported (use an Ollama or OpenAI-compatible backend)."
    )


def occ_cloud_env_lines(config: DannoConfig, value: str) -> list[str]:
    """The env-file lines that point occ at a cloud model's provider, or [].

    occ reaches an OpenAI-compatible provider via `OPENAI_BASE_URL` + `OPENAI_API_KEY`
    (its OpenAI path), so a cloud [models] name emits BOTH: the backend's `base_url` and
    the host value of its `api_key_env` mapped onto `OPENAI_API_KEY`. They land only in the
    chmod-600 env-file (never a command line). Fails loud (Working Rule 8) when the key var
    is unset. Empty for local Ollama (the relay path sets `OPENAI_BASE_URL` inline) and for
    raw `ollama/…` refs."""
    if "/" in value:
        return []
    model = config.models.get(value)
    if model is None:
        return []
    backend = config.backends[model.backend]
    if not isinstance(backend, OpenAIBackend):
        return []
    key = os.environ.get(backend.api_key_env)
    if not key:
        raise CommandFailedError(
            f"--harness occ -m {value} needs the cloud provider key '{backend.api_key_env}', but "
            f"it is unset in danno's environment. Export {backend.api_key_env} and re-run."
        )
    return [f"OPENAI_BASE_URL={backend.base_url}", f"OPENAI_API_KEY={key}"]


def resolve_model_for_harness(target_abs: Path, harness: str, value: str) -> str:
    """The `-m/--model` flow for `sandbox start`: load danno.toml and resolve `value`
    for `harness`. Supported only for the danno-clone harnesses claurst and occ — claude has
    its own `--model` and opencode's model comes from the generated opencode.jsonc, so `-m`
    with either fails loud rather than being silently ignored. Raises `DannoConfigError`
    (bad toml) or `CommandFailedError`."""
    if harness not in (CLAURST_HARNESS, OCC_HARNESS):
        raise CommandFailedError(
            "`-m/--model` on `danno sandbox start` is only supported with `--harness claurst` "
            "or `--harness occ`. Claude Code uses its own `--model` (pass it after `--`); "
            "opencode's model comes from danno.toml. Re-run without `-m`."
        )
    config = load_config(target_abs / "danno.toml")
    if harness == OCC_HARNESS:
        return resolve_occ_model(config, value)
    return resolve_claurst_model(config, value)


def resolve_claurst_start(target_abs: Path, harness: str, value: str) -> tuple[str, list[str]]:
    """`sandbox start -m <value>`: the claurst `-m` ref PLUS any cloud-key env-file lines.

    Loads danno.toml once and returns `(ref, env_lines)` — `env_lines` is empty for local
    Ollama and `["<VAR>=<value>"]` for a cloud model (fail loud if the host var is unset)."""
    config = load_config(target_abs / "danno.toml")
    return resolve_claurst_model(config, value), claurst_cloud_env_lines(config, value)


def resolve_occ_start(target_abs: Path, harness: str, value: str) -> tuple[str, list[str]]:
    """`sandbox start -m <value>` for occ: the occ `<backend>/<tag>` ref PLUS any cloud
    env-file lines (`OPENAI_BASE_URL` + `OPENAI_API_KEY`; empty for local Ollama)."""
    config = load_config(target_abs / "danno.toml")
    return resolve_occ_model(config, value), occ_cloud_env_lines(config, value)


def resolve_start(target_abs: Path, harness: str, value: str) -> tuple[str, list[str]]:
    """Dispatch `sandbox start -m` to the per-harness resolver. Non-clone harnesses fail loud
    via `resolve_model_for_harness` (which rejects `-m` for claude/opencode)."""
    if harness == OCC_HARNESS:
        return resolve_occ_start(target_abs, harness, value)
    if harness == CLAURST_HARNESS:
        return resolve_claurst_start(target_abs, harness, value)
    resolve_model_for_harness(target_abs, harness, value)  # raises the non-clone rejection
    raise AssertionError("unreachable")  # resolve_model_for_harness always raises here


def _build_env_file(harness_lines: list[str], env_pairs: list[str], env_files: list[str]) -> Path:
    """Combine --env-file(s), --env pairs, and the harness env lines into one 0600 temp file."""
    fd, path = tempfile.mkstemp(prefix="danno-env-")
    os.close(fd)
    p = Path(path)
    p.chmod(0o600)
    lines: list[str] = []
    for f in env_files:
        lines.append(Path(f).read_text(encoding="utf-8").rstrip("\n"))
    lines.extend(env_pairs)
    lines.extend(harness_lines)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


_ENV_REF = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def _required_env_refs(target_abs: Path) -> set[str]:
    """Env var names referenced as `{env:VAR}` in the project's opencode.jsonc.

    Scans the raw text (regex, comment-agnostic) so it catches every `{env:…}` the
    sandboxed opencode will try to resolve — e.g. an `openai` backend's api key."""
    cfg = target_abs / ".opencode" / "opencode.jsonc"
    if not cfg.is_file():
        return set()
    return set(_ENV_REF.findall(cfg.read_text(encoding="utf-8")))


def _provided_env(env_pairs: list[str], env_files: list[str]) -> dict[str, str]:
    """KEY→VAL the user is injecting via --env / --env-file (later entries win)."""
    provided: dict[str, str] = {}
    for f in env_files:
        for line in Path(f).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                provided[key] = val
    for pair in env_pairs:
        if "=" in pair:
            key, val = pair.split("=", 1)
            provided[key] = val
    return provided


def resolve_env_refs(
    target_abs: Path,
    env_pairs: list[str],
    env_files: list[str],
) -> tuple[list[str], list[str]]:
    """Resolve the env vars opencode needs, WITHOUT raising — the shared core of
    `reconcile_env_refs` (which raises) and the validator sweep (which warns).

    The required set is every `{env:VAR}` referenced in opencode.jsonc (e.g. an
    openai-compatible backend's `api_key_env`). For each: keep the explicitly-passed
    value, else auto-inject the host-exported one, else record it as missing.
    Returns `(augmented_pairs, missing)` — `augmented_pairs` is `env_pairs` plus the
    host-sourced additions; `missing` lists vars neither passed nor host-exported
    (and ones passed empty, the footgun)."""
    required = _required_env_refs(target_abs)
    if not required:
        return list(env_pairs), []
    provided = _provided_env(env_pairs, env_files)
    augmented = list(env_pairs)
    missing: list[str] = []
    for name in sorted(required):
        if provided.get(name):  # explicitly passed, non-empty
            continue
        if name in provided:  # passed but empty — the exact footgun we hit
            missing.append(name)
            continue
        host = os.environ.get(name)
        if host:
            augmented.append(f"{name}={host}")
            log_info(f"injecting {name} from danno's host environment")
        else:
            missing.append(name)
    return augmented, missing


def reconcile_env_refs(target_abs: Path, env_pairs: list[str], env_files: list[str]) -> list[str]:
    """Fail loud (Working Rule 8) if opencode.jsonc references an env var that isn't
    supplied, and auto-inject any that are exported in danno's host environment.

    Returns env_pairs augmented with host values for referenced vars the user did
    not pass explicitly; raises `CommandFailedError` listing any that are neither
    passed via --env/--env-file nor present (non-empty) in the host environment —
    so a missing key fails up front instead of as a deep opencode 'authorization
    missing' error."""
    augmented, missing = resolve_env_refs(target_abs, env_pairs, env_files)
    if missing:
        names = ", ".join(missing)
        raise CommandFailedError(
            f"opencode.jsonc needs env var(s) not supplied: {names}. Export them "
            f"(e.g. `export {missing[0]}=…`) or pass `--env {missing[0]}=…` to "
            f"`danno sandbox start`."
        )
    return augmented


def _maybe_load_config(target_abs: Path) -> DannoConfig | None:
    """Load `{target}/danno.toml` for env assembly, or None if it is absent.

    A config-less `danno sandbox shell`/`start` on a bare directory still works — it
    simply gets no `[env]` overlay (only harness defaults + CLI env)."""
    cfg = target_abs / "danno.toml"
    if not cfg.is_file():
        return None
    return load_config(cfg)


def _resolve_env_indirection(value: str) -> tuple[str, list[str]]:
    """Resolve `{env:VAR}` host-indirection inside an `[env]` value.

    Returns `(resolved, missing)` where `missing` names any referenced host var that
    is unset/empty (each expands to '' in `resolved`; the caller decides raise vs skip)."""
    missing: list[str] = []

    def sub(m: re.Match[str]) -> str:
        var = m.group(1)
        host = os.environ.get(var)
        if not host:
            missing.append(var)
            return ""
        return host

    return _ENV_REF.sub(sub, value), missing


def assemble_harness_env(
    config: DannoConfig | None,
    *,
    harness_defaults: list[str],
    env_pairs: list[str],
    env_files: list[str],
    strict: bool = False,
) -> list[str]:
    """Final `KEY=VAL` env-file lines for a config-driven harness (opencode/claurst/occ),
    applying the locked precedence (highest wins):

        CLI (--env / --env-file)  >  host os.environ  >  danno.toml [env]  >  harness default

    The host-env tier is deliberately scoped to keys the operator opted into managing
    (those named in `[env]` or on the CLI): a bare same-named host var does NOT clobber
    a computed harness default (e.g. `OLLAMA_BASE_URL`), which would silently break the
    sandbox's Ollama networking. A `{env:VAR}` reference inside an `[env]` value resolves
    from `os.environ`; an unset reference raises in `strict` mode (Working Rule 8) or
    warns + drops the key otherwise. Claude does NOT flow through here — its auth stays
    in `harness_env` exactly as-is."""
    merged: dict[str, str] = {}
    for line in harness_defaults:  # level 4: code default
        if "=" in line:
            key, val = line.split("=", 1)
            merged[key] = val
    literal = dict(config.env) if config is not None else {}
    for key, raw in literal.items():
        host = os.environ.get(key)
        if host:  # level 2: an exported host var overrides the committed [env] value
            if merged.get(key) != host:
                log_info(f"[env] {key}: using danno's host environment value")
            merged[key] = host
            continue
        resolved, missing = _resolve_env_indirection(raw)  # level 3: [env] literal
        if missing:
            names = ", ".join(missing)
            if strict:
                raise CommandFailedError(
                    f"danno.toml [env] {key} references host env var(s) not set: {names}. "
                    f"Export them (e.g. `export {missing[0]}=…`) or drop the {{env:…}} reference."
                )
            log_warn(
                f"danno.toml [env] {key} references unset host var(s) {names}; dropping {key}."
            )
            continue
        merged[key] = resolved
    merged.update(_provided_env(env_pairs, env_files))  # level 1: CLI (highest)
    return [f"{k}={v}" for k, v in merged.items()]


def _exec_session(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    harness: str,
    ollama_url: str,
    env_pairs: list[str],
    env_files: list[str],
    home: Path | None,
    container_argv: list[str],
    why: str,
) -> list[str]:
    """Set up the in-container session and exec `container_argv` inside it.

    SYNC REQUIREMENT: this is the single shared core of `launch` (runs the harness) and
    `shell` (runs bash). `danno sandbox shell` MUST stay environmentally identical to
    `danno sandbox start`: same mounted repo working dir (`-w <target>`), same
    chmod-600 env-file (harness auth / Ollama URL / relocated config home / resolved
    `{env:VAR}` refs), same claude onboarding seeding — differing ONLY in
    `container_argv` (the harness binary vs `bash`). Both callers route through here so
    the two paths cannot drift; never add env/`-w`/mount wiring to one path without
    the other.

    With a persistent `home`, the harness's config is relocated onto it; for claude,
    onboarding and workspace trust are pre-seeded so neither wizard nor the trust
    dialog blocks the session. The exec runs with `check=False`: quitting the TUI or
    exiting the shell is not a danno error."""
    if harness == "claude" and home is not None:
        seed_onboarding(home, target_abs)
    if harness == "claude":
        # claude's auth injection stays exactly as-is (it is NOT danno.toml-config
        # driven); harness_env lines win over --env/--env-file via _build_env_file.
        lines = harness_env(harness, ollama_url, home)
        env_path_lines, env_path_pairs, env_path_files = lines, env_pairs, env_files
    else:
        if harness == DEFAULT_HARNESS:
            # opencode reads opencode.jsonc; verify every {env:VAR} it references is
            # supplied (auto-injecting host-exported ones), else fail loud up front.
            env_pairs = reconcile_env_refs(target_abs, env_pairs, env_files)
        # opencode/claurst/occ: fold harness defaults, danno.toml [env], host env and CLI
        # into one precedence-ordered file. assemble_harness_env already applied
        # env_pairs/env_files, so pass them empty to _build_env_file (avoid double-apply).
        lines = assemble_harness_env(
            _maybe_load_config(target_abs),
            harness_defaults=harness_env(harness, ollama_url, home),
            env_pairs=env_pairs,
            env_files=env_files,
        )
        env_path_lines, env_path_pairs, env_path_files = lines, [], []
    injected = ", ".join(line.split("=", 1)[0] for line in lines)
    log_info(f"injecting {injected} via a chmod-600 --env-file")
    env_path = _build_env_file(env_path_lines, env_path_pairs, env_path_files)
    cmd = [
        *sandbox_cli.base(),
        "exec",
        "-it",
        "-w",
        str(target_abs),
        "--env-file",
        str(env_path),
        name,
        *container_argv,
    ]
    try:
        return runner.run(cmd, why=why, check=False)
    finally:
        env_path.unlink(missing_ok=True)


def launch(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    harness: str = DEFAULT_HARNESS,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    env_pairs: list[str] | None = None,
    env_files: list[str] | None = None,
    home: Path | None = None,
    harness_args: list[str] | None = None,
    model: str | None = None,
    capture_relay_port: int | None = None,
) -> list[str]:
    """Launch the in-container harness in the mounted repo, wired to host Ollama /
    harness auth. Launching is the command's purpose, so it always executes (not gated
    by `--apply`). The session setup is shared with `shell` via `_exec_session` (see
    its SYNC REQUIREMENT); this path's only specialisation is the container command —
    the harness binary plus `harness_args` forwarded verbatim (e.g. `["--resume", "<id>"]`
    for `claude`).

    claurst is the exception: it can't reach host Ollama directly (its Rust client
    ignores the egress proxy), so its command is the relay-bracketed
    `bash -lc` script from `claurst.interactive_launch_script` (mirrors the headless
    path), with `model` resolved to its `-m ollama/<tag>`. `model` and
    `capture_relay_port` (the `--capture` recording-proxy port) are claurst-only."""
    if harness == CLAURST_HARNESS:
        from danno_validator.claurst import interactive_launch_script  # local: import cycle

        container_argv = interactive_launch_script(
            model, harness_args or [], capture_port=capture_relay_port
        )
    elif harness == OCC_HARNESS:
        from danno_validator.occ import interactive_launch_script as occ_launch_script

        container_argv = occ_launch_script(
            model, harness_args or [], capture_port=capture_relay_port
        )
    else:
        container_argv = [harness, *(harness_args or [])]
    return _exec_session(
        runner,
        name,
        target_abs,
        harness=harness,
        ollama_url=ollama_url,
        env_pairs=env_pairs or [],
        env_files=env_files or [],
        home=home,
        container_argv=container_argv,
        why=f"launch {harness} in sandbox '{name}'",
    )


def _emit_claurst_config(runner: Runner, target_abs: Path, home: Path) -> list[GenerateResult]:
    """Generate claurst's registry overlay + settings.json agents into `{home}/.claurst`.

    The claurst peer of install's opencode `_emit_config`: Tier-1 advise/apply, and a loud
    warning (Working Rule 8) for any [agents] field claurst can't express. The overlay
    fixes claurst's tool-gating + context window from danno.toml (Bug 4/7) for the local
    Ollama path; cloud model SELECTION is still gated by `resolve_claurst_model`."""
    config = load_config(target_abs / "danno.toml")
    results = generate_claurst(config, home / _CLAURST_DIR, apply=runner.apply)
    for result in results:
        for warning in result.warnings:
            log_warn(warning)
        if result.action is Action.WROTE:
            log_info(f"[green]wrote[/green] {result.path}")
        elif result.action is Action.UNCHANGED:
            log_info(f"unchanged: {result.path}")
        else:  # DIFF — would change an existing file; needs --apply
            log_info(f"[yellow]{result.path} would change[/yellow]; re-run with --apply.")
    return results


def _ensure_provisioned(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    harness: str,
    allow_hosts: tuple[str, ...],
    home: Path | None,
    registry_path: Path | None,
) -> None:
    """The pre-session gate shared by `start` and `shell`: under `--apply`, provision
    (idempotent); otherwise require the sandbox to already exist, failing loud
    (Working Rule 8) rather than letting `docker sandbox exec` error on a missing
    sandbox. Kept in one place so both interactive commands gate identically."""
    if runner.apply:
        provision(
            runner,
            name,
            target_abs,
            harness=harness,
            allow_hosts=allow_hosts,
            home=home,
            registry_path=registry_path,
        )
    elif not sandbox_exists(name):
        raise CommandFailedError(
            f"sandbox '{name}' is not provisioned. Run `danno sandbox start --apply` "
            f"(provisions then launches) or `danno install --apply` first."
        )
    # claurst reads danno's generated config from its relocated HOME; emit it before the
    # session so the launched (or hand-run) claurst sees the right models/agents. Both
    # start and shell pass through here, keeping them in sync (SYNC REQUIREMENT). Needs a
    # persistent agent_home (an ephemeral VM-local HOME has nowhere host-side to write).
    if harness == CLAURST_HARNESS and home is not None:
        _emit_claurst_config(runner, target_abs, home)


@dataclass(frozen=True)
class _CaptureWiring:
    """What `_capture_session` yields: the egress allow-list to provision with, plus —
    for claurst only — the host port its in-VM relay must forward to so its Ollama wire
    traffic is recorded. `relay_upstream_port` is None for opencode (it dials the proxy
    via its rewritten `base_url`) and when capture is off."""

    allow_hosts: tuple[str, ...]
    relay_upstream_port: int | None = None


def _claurst_relay_capture_port(targets: Sequence[CaptureTarget]) -> int:
    """The capture-proxy port the claurst relay must forward to so its Ollama wire
    traffic is recorded: the target re-originating to the host Ollama the relay normally
    dials (`host.docker.internal:11434` → `127.0.0.1:11434`, host-side). Fails loud
    (Working Rule 8) when no such backend exists or more than one is ambiguous."""
    from danno_validator.driver import CLAURST_RELAY_DEFAULT_UPSTREAM_PORT

    host_ollama = f"http://127.0.0.1:{CLAURST_RELAY_DEFAULT_UPSTREAM_PORT}"
    matches = [t for t in targets if t.upstream == host_ollama]
    if not matches:
        raise CommandFailedError(
            "--capture --harness claurst: no Ollama backend fronting host Ollama "
            "(host.docker.internal:11434) to record through; claurst's relay only reaches "
            "local Ollama. Point a danno.toml [backends.*] ollama backend at it."
        )
    if len(matches) > 1:
        raise CommandFailedError(
            f"--capture --harness claurst: {len(matches)} Ollama backends front host Ollama; "
            "the relay can record through only one — disambiguate danno.toml [backends]."
        )
    return matches[0].proxy_port


@contextmanager
def _capture_session(
    runner: Runner,
    target_abs: Path,
    *,
    harness: str,
    capture_dir: Path | None,
    base_allow_hosts: tuple[str, ...],
) -> Iterator[_CaptureWiring]:
    """`--capture` for `start`/`shell`: run per-backend recording proxies and yield the
    `_CaptureWiring` to provision/launch with (egress allow-list + claurst relay port).

    Two levers, by harness: opencode dials the proxies via its generated opencode.jsonc
    `base_url`s (transiently rewritten here, restored byte-for-byte on exit); claurst
    ignores that config and the egress proxy, so instead its in-VM relay is pointed at
    the Ollama proxy (`relay_upstream_port`, wired into the launch). A no-op yielding
    `base_allow_hosts` when capture is off; warns + no-ops for any other harness. Requires
    `--apply`: the per-run proxy ports must be opened in the sandbox egress (a
    re-provision)."""
    if capture_dir is None:
        yield _CaptureWiring(base_allow_hosts)
        return
    if harness not in (DEFAULT_HARNESS, CLAURST_HARNESS, OCC_HARNESS):
        log_warn(
            f"--capture supports the '{DEFAULT_HARNESS}', '{CLAURST_HARNESS}' and '{OCC_HARNESS}' "
            f"harnesses; not capturing '{harness}'."
        )
        yield _CaptureWiring(base_allow_hosts)
        return
    if not runner.apply:
        raise CommandFailedError(
            "--capture needs --apply: the recording proxies use per-run ports that must be "
            "opened in the sandbox egress (a re-provision)."
        )
    config = load_config(target_abs / "danno.toml")
    cfg_for_run, targets = plan_capture(config, capture_dir)
    uncap = uncaptured_cloud_refs(config)
    if uncap:
        log_warn(
            "--capture cannot record built-in cloud refs (no danno base_url lever): "
            f"{', '.join(uncap)}"
        )
    allow = capture_allow_hosts(targets, base_allow_hosts)
    if harness in (CLAURST_HARNESS, OCC_HARNESS):
        # claurst/occ read neither opencode.jsonc nor the egress proxy; both dial an in-VM
        # relay (occ reuses claurst's `_claurst_script` bracket). Point that relay at the
        # Ollama recording proxy (no opencode.jsonc rewrite).
        relay_port = _claurst_relay_capture_port(targets)
        log_info(f"--capture: recording {harness}<->Ollama wire traffic to {capture_dir}")
        with captures_running(targets):
            yield _CaptureWiring(allow, relay_port)
        return
    log_info(f"--capture: recording opencode<->backend wire traffic to {capture_dir}")
    jsonc = target_abs / ".opencode" / "opencode.jsonc"
    snapshot = jsonc.read_text(encoding="utf-8") if jsonc.is_file() else None
    generate(cfg_for_run, target_abs, apply=True)  # rewrite baseURLs to the proxies
    try:
        with captures_running(targets):
            yield _CaptureWiring(allow)
    finally:
        if snapshot is not None:
            jsonc.write_text(snapshot, encoding="utf-8")  # restore the user's config exactly
        elif jsonc.is_file():
            jsonc.unlink()


def start(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    harness: str = DEFAULT_HARNESS,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
    env_pairs: list[str] | None = None,
    env_files: list[str] | None = None,
    home: Path | None = None,
    registry_path: Path | None = None,
    harness_args: list[str] | None = None,
    capture_dir: Path | None = None,
    model: str | None = None,
) -> None:
    """Provision (under `--apply`, idempotent) then launch the in-container AGENT.

    SYNC REQUIREMENT: `start` and `shell` are deliberately the same command with a
    different last step — `start` runs the harness, `shell` runs `bash`. Both gate via
    `_ensure_provisioned`, set up the session via `_exec_session`, and wrap the span in
    `_capture_session` (`--capture`); put any new provisioning/env/mount/capture
    behaviour in those shared helpers, not in one command only, so the two cannot drift.

    `harness_args` are forwarded verbatim to the harness binary (e.g. `--resume <id>`).
    `model` is the resolved, locality-checked claurst `-m ollama/<tag>` (claurst-only;
    see `resolve_model_for_harness`); it reaches the harness command via `launch`."""
    with _capture_session(
        runner, target_abs, harness=harness, capture_dir=capture_dir, base_allow_hosts=allow_hosts
    ) as cap:
        _ensure_provisioned(
            runner,
            name,
            target_abs,
            harness=harness,
            allow_hosts=cap.allow_hosts,
            home=home,
            registry_path=registry_path,
        )
        launch(
            runner,
            name,
            target_abs,
            harness=harness,
            ollama_url=ollama_url,
            env_pairs=env_pairs,
            env_files=env_files,
            home=home,
            harness_args=harness_args,
            model=model,
            capture_relay_port=cap.relay_upstream_port,
        )


def exec_in_container(runner: Runner, name: str, command: str, *, why: str) -> list[str]:
    """Advise (and under --apply, run) a shell command inside the sandbox VM.

    Non-tty (`bash -lc`, no `-it`) so it works headless / under --apply. `exec`
    auto-starts a created-but-stopped sandbox, so this needs no explicit `start`.
    """
    return runner.advise([*sandbox_cli.base(), "exec", name, "bash", "-lc", command], why=why)


def run_npm_setup(runner: Runner, name: str, plugins: list[NpmPlugin]) -> list[list[str]]:
    """Run each `[[npm]]` plugin's optional in-container `setup` commands.

    The plugins themselves are auto-installed by OpenCode from the generated
    opencode.jsonc `"plugin"` array; only a plugin's extra `setup` steps (e.g.
    plannotator's slash-command installer) need an in-container exec.
    """
    cmds: list[list[str]] = []
    for plugin in plugins:
        for command in plugin.setup:
            cmds.append(
                exec_in_container(
                    runner, name, command, why=f"run {plugin.package} setup in sandbox '{name}'"
                )
            )
    return cmds


def shell(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    harness: str = DEFAULT_HARNESS,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
    env_pairs: list[str] | None = None,
    env_files: list[str] | None = None,
    home: Path | None = None,
    registry_path: Path | None = None,
    capture_dir: Path | None = None,
) -> list[str]:
    """Open an interactive bash shell inside the sandbox VM — `start` minus the harness
    launch.

    SYNC REQUIREMENT: see `start`. `shell` MUST stay identical to `start` except for
    the final in-container command: same provisioning gate (`_ensure_provisioned`),
    same `--capture` wrap (`_capture_session`), and same session setup (`_exec_session`:
    `-w <target>`, the env-file with harness auth / Ollama URL / relocated config home /
    resolved `{env:}` refs), so a tool you run by hand from this shell is wired exactly
    as `start` would wire the harness. The ONLY difference is the container command —
    `bash` instead of the harness binary."""
    with _capture_session(
        runner, target_abs, harness=harness, capture_dir=capture_dir, base_allow_hosts=allow_hosts
    ) as cap:
        _ensure_provisioned(
            runner,
            name,
            target_abs,
            harness=harness,
            allow_hosts=cap.allow_hosts,
            home=home,
            registry_path=registry_path,
        )
        return _exec_session(
            runner,
            name,
            target_abs,
            harness=harness,
            ollama_url=ollama_url,
            env_pairs=env_pairs or [],
            env_files=env_files or [],
            home=home,
            container_argv=["bash"],
            why=f"open a shell in sandbox '{name}'",
        )


def ls(registry_path: Path | None = None) -> None:
    """Read-only: print each recorded `name → target (harness)` and whether it is
    currently live per `docker sandbox ls`. Answers 'which container is this?'"""
    registry_path = registry_path or registry.default_path()
    entries = registry.load(registry_path)
    if not entries:
        log_info(f"no sandboxes recorded in {registry_path}")
        return
    live = live_sandbox_names()
    for name in sorted(entries):
        info = entries[name]
        status = "live" if name in live else "not live"
        log_info(f"{name} → {info.get('target')} ({info.get('harness')}) [{status}]")


def rebuild(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    harness: str = DEFAULT_HARNESS,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
    home: Path | None = None,
    registry_path: Path | None = None,
) -> list[list[str]]:
    """Recycle the sandbox: remove it (if present), then re-provision from scratch.

    `docker sandbox rm` takes no force flag and errors on a missing sandbox, so
    under --apply we stop-then-remove only when it actually exists. The harness home
    (c) lives on the host, so it survives this recycle.
    """
    cmds: list[list[str]] = []
    if not _live(runner) or sandbox_exists(name):
        # Stop first so rm doesn't trip on a running VM, then remove.
        cmds.append(stop(runner, name))
        cmds.append(runner.advise(sandbox_cli.rm_argv(name), why=f"remove sandbox '{name}'"))
    cmds += provision(
        runner,
        name,
        target_abs,
        harness=harness,
        allow_hosts=allow_hosts,
        home=home,
        registry_path=registry_path,
    )
    return cmds


def update(runner: Runner, name: str, harness: str = DEFAULT_HARNESS) -> list[str]:
    """Advise how to update the harness inside the container.

    The harness ships in Docker Desktop's prebuilt sandbox image, so the durable
    update path is recreating the sandbox on a newer image; this advises the
    in-container self-update as the quick path.
    """
    if harness == "claude":
        log_info(
            "Claude Code ships in Docker's prebuilt sandbox image; for a full update, "
            "`danno sandbox rebuild --harness claude` after updating Docker Desktop."
        )
        return runner.advise(
            [*sandbox_cli.base(), "exec", name, "claude", "update"],
            why=f"update Claude Code inside sandbox '{name}'",
        )
    log_info(
        "OpenCode ships in Docker's prebuilt sandbox image; for a full update, "
        "`danno sandbox rebuild` after updating Docker Desktop."
    )
    return runner.advise(
        [*sandbox_cli.base(), "exec", name, "opencode", "upgrade"],
        why=f"update OpenCode inside sandbox '{name}'",
    )
