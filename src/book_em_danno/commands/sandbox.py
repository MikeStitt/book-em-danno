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
from ..config.generate import generate, model_ref
from ..config.loader import DannoConfigError, load_config
from ..config.schema import DannoConfig, NpmPlugin, OllamaBackend, Sandbox
from ..core import registry
from ..core.exec import CommandFailedError, Runner, log_info, log_warn
from . import ollama

DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434/v1"
DEFAULT_ALLOW_HOSTS = ("localhost:11434",)
DEFAULT_AGENT = "opencode"
# Auth env vars Claude Code accepts, in preference order (subscription token first).
CLAUDE_AUTH_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
# claurst (a pure-Rust Claude-Code clone) is NOT a prebuilt `docker sandbox` image: it
# is hosted in the `shell` image and the release binary is installed post-create (see
# `danno_validator.claurst.install_claurst`). The logical agent label stays "claurst"
# everywhere (naming/registry/env/launch); only the create-time Docker image differs.
CLAURST_AGENT = "claurst"


def _docker_image(agent: str) -> str:
    """The prebuilt `docker sandbox` image backing a logical agent label.

    Almost always the label itself (opencode/claude/… ARE images). claurst is the
    exception — it has no prebuilt image, so it rides the `shell` image and is
    installed afterwards; the label is preserved for the sandbox name and registry."""
    if agent == CLAURST_AGENT:
        from danno_validator.claurst import CLAURST_SANDBOX_IMAGE  # local: avoids import cycle

        return CLAURST_SANDBOX_IMAGE
    return agent


def default_name(target_abs: Path, agent: str = DEFAULT_AGENT) -> str:
    """Sandbox name derived from the project's *parent and own* dir names.

    `danno-<parent>-<base>` so same-basename projects in different parents
    (`~/work/acme` vs `~/clients/acme`) and worktree dirs (`…/main`, `…/login`)
    stay distinct. The default opencode agent keeps the bare name; a per-agent
    suffix otherwise so agents get separate sandboxes that can coexist.
    """
    parent = target_abs.parent.name
    stem = f"{parent}-{target_abs.name}" if parent else target_abs.name
    base = f"danno-{stem}"
    return base if agent == DEFAULT_AGENT else f"{base}-{agent}"


def live_sandbox_names() -> set[str]:
    """The set of sandbox names `docker sandbox ls` reports (empty if unavailable)."""
    try:
        out = subprocess.run(
            ["docker", "sandbox", "ls"], capture_output=True, text=True, check=False
        ).stdout
    except (FileNotFoundError, OSError):
        return set()
    return {line.split()[0] for line in out.splitlines()[1:] if line.split()}


def sandbox_exists(name: str) -> bool:
    """True if a sandbox named `name` is listed by `docker sandbox ls`."""
    return name in live_sandbox_names()


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
            f"agent home {home} is inside the repo {target_abs} — chat history would "
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
    agent: str = DEFAULT_AGENT,
    *,
    home: Path | None = None,
    registry_path: Path | None = None,
) -> list[str]:
    """Create the sandbox, mounting the project at the same path inside the VM.

    `agent` selects the Docker prebuilt agent baked into the VM (opencode, claude,
    …). When `home` is set, the host agent-home dir is mounted as a second
    workspace (and `mkdir -p`'d first so the mount has a source). With
    `registry_path`, the name→target mapping is recorded under --apply (advised
    otherwise) and a loud warning fires if `name` already maps elsewhere.
    Idempotent under --apply: an already-existing sandbox is left in place.
    """
    if registry_path is not None:
        existing = registry.lookup(registry_path, name)
        if existing is not None and existing.get("target") != str(target_abs):
            log_warn(
                f"sandbox name '{name}' already maps to {existing['target']}; creating it "
                f"for {target_abs} would collide — pass --name to disambiguate."
            )

    cmd = ["docker", "sandbox", "create", "--name", name, _docker_image(agent), str(target_abs)]
    if home is not None:
        cmd.append(str(home))

    if _live(runner) and sandbox_exists(name):
        log_info(f"sandbox '{name}' already exists — skipping create")
    else:
        if home is not None:
            runner.advise(["mkdir", "-p", str(home)], why=f"ensure agent home {home} exists")
        runner.advise(cmd, why=f"create the {agent} sandbox '{name}' for {target_abs}")

    if registry_path is not None:
        if _live(runner):
            registry.record(registry_path, name, str(target_abs), agent)
        else:
            log_info(f"would record '{name}' → {target_abs} in {registry_path}")
    return cmd


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


def ensure_running(runner: Runner, name: str) -> list[str]:
    """Start a stopped-but-existing sandbox VM. `docker sandbox` has no `start`
    subcommand, but `exec … true` auto-starts the VM (then exits). Needed before
    `network proxy`, which 400s ("not running") against a stopped VM — the case hit
    when re-provisioning an existing sandbox left stopped by a prior provision."""
    return runner.advise(
        ["docker", "sandbox", "exec", name, "true"],
        why=f"ensure sandbox '{name}' is running before configuring its network",
    )


def provision(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    agent: str = DEFAULT_AGENT,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
    home: Path | None = None,
    registry_path: Path | None = None,
) -> list[list[str]]:
    """Get the sandbox to 'ready': create + egress hole + stop (so the policy applies
    on next start). Does NOT launch the TUI — that's `start`."""
    ollama.announce_loopback()
    if agent == DEFAULT_AGENT and not (target_abs / ".opencode" / "opencode.jsonc").is_file():
        log_info(
            "[yellow]WARN[/yellow] target has no .opencode/opencode.jsonc — "
            "run `danno install` first so the sandbox has a config to load."
        )
    # A fresh `create` leaves the VM running, so `configure_proxy` works. But on a
    # re-provision `create` is skipped and the existing VM is stopped (a prior
    # provision ends with `stop`), and `network proxy` 400s against a stopped VM —
    # so bring it back up first when the sandbox already existed.
    preexisting = _live(runner) and sandbox_exists(name)
    cmds = [create(runner, name, target_abs, agent, home=home, registry_path=registry_path)]
    if preexisting:
        cmds.append(ensure_running(runner, name))
    cmds.append(configure_proxy(runner, name, allow_hosts))
    # The network policy only takes on a fresh VM start; stop so the next `start`
    # applies the allow-rule.
    cmds.append(stop(runner, name))
    if agent == CLAURST_AGENT:
        # claurst has no prebuilt image: drop its binary into the `shell` VM. Done AFTER
        # the stop so the install exec auto-starts the VM with the allow-policy armed
        # (apt + the GitHub release fetch need egress). Idempotent (self-skips when
        # already installed). The binary stays VM-local; only ~/.claurst is persisted
        # via the relocated HOME at launch (see agent_env). Local import: claurst.py
        # imports back into this module.
        from danno_validator.claurst import install_claurst

        cmds.append(install_claurst(runner, name))
    return cmds


def agent_env(agent: str, ollama_url: str, home: Path | None = None) -> list[str]:
    """The agent-specific `KEY=VAL` env-file lines.

    opencode reaches host Ollama via OLLAMA_BASE_URL. claude needs auth: prefer the
    subscription token (CLAUDE_CODE_OAUTH_TOKEN), else ANTHROPIC_API_KEY, read from
    danno's host environment. Fail loud (Working Rule 8) when neither is set. The
    secret only ever lands in the chmod-600 env-file, never on the command line.

    When `home` is set, the agent's global config is relocated onto the mounted
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
    Ollama line here.
    """
    if agent == CLAURST_AGENT:
        return [f"HOME={home}"] if home is not None else []
    if agent == "claude":
        lines: list[str] = []
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
    """Resolve a `-m` value to claurst's `-m ollama/<tag>`, rejecting cloud loudly.

    `value` is a `[models]` name (the documented form, e.g. `gemma4`) or a raw
    `provider/model` ref. claurst is local-only — its Rust client ignores the egress
    proxy, and the in-VM relay reaches only host Ollama — so any non-Ollama target is
    unreachable and MUST fail loud (Working Rule 8), naming the provider, rather than
    launch and silently fail mid-session."""
    if "/" in value:
        provider = value.split("/", 1)[0]
        if provider != "ollama":
            raise CommandFailedError(
                f"--agent claurst is local-only and cannot reach the '{provider}' provider "
                f"in '{value}' (claurst's client ignores the sandbox egress proxy). Pass an "
                f"Ollama model (a [models] entry on an ollama backend, or `ollama/<tag>`)."
            )
        return value
    model = config.models.get(value)
    if model is None:
        raise CommandFailedError(
            f"model '{value}' is not defined in danno.toml [models]. "
            f"Pass a local Ollama model for --agent claurst."
        )
    backend = config.backends[model.backend]
    if not isinstance(backend, OllamaBackend):
        raise CommandFailedError(
            f"--agent claurst is local-only, but model '{value}' is on the '{backend.kind}' "
            f"backend '{model.backend}' (not Ollama), which claurst cannot reach from the "
            f"sandbox (its client ignores the egress proxy). Pick an Ollama model."
        )
    return model_ref(config, value)


def resolve_model_for_agent(target_abs: Path, agent: str, value: str) -> str:
    """The `-m/--model` flow for `sandbox start`: load danno.toml and resolve `value`
    for `agent`. claurst-only — claude has its own `--model` and opencode's model comes
    from the generated opencode.jsonc, so `-m` with either fails loud rather than being
    silently ignored. Raises `DannoConfigError` (bad toml) or `CommandFailedError`."""
    if agent != CLAURST_AGENT:
        raise CommandFailedError(
            "`-m/--model` on `danno sandbox start` is only supported with `--agent claurst`. "
            "Claude Code uses its own `--model` (pass it after `--`); opencode's model comes "
            "from danno.toml. Re-run without `-m`."
        )
    config = load_config(target_abs / "danno.toml")
    return resolve_claurst_model(config, value)


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


def _exec_session(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    agent: str,
    ollama_url: str,
    env_pairs: list[str],
    env_files: list[str],
    home: Path | None,
    container_argv: list[str],
    why: str,
) -> list[str]:
    """Set up the in-container session and exec `container_argv` inside it.

    SYNC REQUIREMENT: this is the single shared core of `launch` (runs the agent) and
    `shell` (runs bash). `danno sandbox shell` MUST stay environmentally identical to
    `danno sandbox start`: same mounted repo working dir (`-w <target>`), same
    chmod-600 env-file (agent auth / Ollama URL / relocated config home / resolved
    `{env:VAR}` refs), same claude onboarding seeding — differing ONLY in
    `container_argv` (the agent binary vs `bash`). Both callers route through here so
    the two paths cannot drift; never add env/`-w`/mount wiring to one path without
    the other.

    With a persistent `home`, the agent's config is relocated onto it; for claude,
    onboarding and workspace trust are pre-seeded so neither wizard nor the trust
    dialog blocks the session. The exec runs with `check=False`: quitting the TUI or
    exiting the shell is not a danno error."""
    if agent == "claude" and home is not None:
        seed_onboarding(home, target_abs)
    if agent == DEFAULT_AGENT:
        # opencode reads opencode.jsonc; verify every {env:VAR} it references is
        # supplied (auto-injecting host-exported ones), else fail loud up front.
        env_pairs = reconcile_env_refs(target_abs, env_pairs, env_files)
    lines = agent_env(agent, ollama_url, home)
    injected = ", ".join(line.split("=", 1)[0] for line in lines)
    log_info(f"injecting {injected} via a chmod-600 --env-file")
    env_path = _build_env_file(lines, env_pairs, env_files)
    cmd = [
        "docker",
        "sandbox",
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
    agent: str = DEFAULT_AGENT,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    env_pairs: list[str] | None = None,
    env_files: list[str] | None = None,
    home: Path | None = None,
    agent_args: list[str] | None = None,
    model: str | None = None,
    capture_relay_port: int | None = None,
) -> list[str]:
    """Launch the in-container agent in the mounted repo, wired to host Ollama /
    agent auth. Launching is the command's purpose, so it always executes (not gated
    by `--apply`). The session setup is shared with `shell` via `_exec_session` (see
    its SYNC REQUIREMENT); this path's only specialisation is the container command —
    the agent binary plus `agent_args` forwarded verbatim (e.g. `["--resume", "<id>"]`
    for `claude`).

    claurst is the exception: it can't reach host Ollama directly (its Rust client
    ignores the egress proxy), so its command is the relay-bracketed
    `bash -lc` script from `claurst.interactive_launch_script` (mirrors the headless
    path), with `model` resolved to its `-m ollama/<tag>`. `model` and
    `capture_relay_port` (the `--capture` recording-proxy port) are claurst-only."""
    if agent == CLAURST_AGENT:
        from danno_validator.claurst import interactive_launch_script  # local: import cycle

        container_argv = interactive_launch_script(
            model, agent_args or [], capture_port=capture_relay_port
        )
    else:
        container_argv = [agent, *(agent_args or [])]
    return _exec_session(
        runner,
        name,
        target_abs,
        agent=agent,
        ollama_url=ollama_url,
        env_pairs=env_pairs or [],
        env_files=env_files or [],
        home=home,
        container_argv=container_argv,
        why=f"launch {agent} in sandbox '{name}'",
    )


def _ensure_provisioned(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    agent: str,
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
            agent=agent,
            allow_hosts=allow_hosts,
            home=home,
            registry_path=registry_path,
        )
    elif not sandbox_exists(name):
        raise CommandFailedError(
            f"sandbox '{name}' is not provisioned. Run `danno sandbox start --apply` "
            f"(provisions then launches) or `danno install --apply` first."
        )


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
            "--capture --agent claurst: no Ollama backend fronting host Ollama "
            "(host.docker.internal:11434) to record through; claurst's relay only reaches "
            "local Ollama. Point a danno.toml [backends.*] ollama backend at it."
        )
    if len(matches) > 1:
        raise CommandFailedError(
            f"--capture --agent claurst: {len(matches)} Ollama backends front host Ollama; "
            "the relay can record through only one — disambiguate danno.toml [backends]."
        )
    return matches[0].proxy_port


@contextmanager
def _capture_session(
    runner: Runner,
    target_abs: Path,
    *,
    agent: str,
    capture_dir: Path | None,
    base_allow_hosts: tuple[str, ...],
) -> Iterator[_CaptureWiring]:
    """`--capture` for `start`/`shell`: run per-backend recording proxies and yield the
    `_CaptureWiring` to provision/launch with (egress allow-list + claurst relay port).

    Two levers, by agent: opencode dials the proxies via its generated opencode.jsonc
    `base_url`s (transiently rewritten here, restored byte-for-byte on exit); claurst
    ignores that config and the egress proxy, so instead its in-VM relay is pointed at
    the Ollama proxy (`relay_upstream_port`, wired into the launch). A no-op yielding
    `base_allow_hosts` when capture is off; warns + no-ops for any other agent. Requires
    `--apply`: the per-run proxy ports must be opened in the sandbox egress (a
    re-provision)."""
    if capture_dir is None:
        yield _CaptureWiring(base_allow_hosts)
        return
    if agent not in (DEFAULT_AGENT, CLAURST_AGENT):
        log_warn(
            f"--capture supports the '{DEFAULT_AGENT}' and '{CLAURST_AGENT}' agents; "
            f"not capturing '{agent}'."
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
    if agent == CLAURST_AGENT:
        # claurst reads neither opencode.jsonc nor the egress proxy; it dials an in-VM
        # relay. Point that relay at the Ollama recording proxy (no opencode.jsonc rewrite).
        relay_port = _claurst_relay_capture_port(targets)
        log_info(f"--capture: recording claurst<->Ollama wire traffic to {capture_dir}")
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
    agent: str = DEFAULT_AGENT,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
    env_pairs: list[str] | None = None,
    env_files: list[str] | None = None,
    home: Path | None = None,
    registry_path: Path | None = None,
    agent_args: list[str] | None = None,
    capture_dir: Path | None = None,
    model: str | None = None,
) -> None:
    """Provision (under `--apply`, idempotent) then launch the in-container AGENT.

    SYNC REQUIREMENT: `start` and `shell` are deliberately the same command with a
    different last step — `start` runs the agent, `shell` runs `bash`. Both gate via
    `_ensure_provisioned`, set up the session via `_exec_session`, and wrap the span in
    `_capture_session` (`--capture`); put any new provisioning/env/mount/capture
    behaviour in those shared helpers, not in one command only, so the two cannot drift.

    `agent_args` are forwarded verbatim to the agent binary (e.g. `--resume <id>`).
    `model` is the resolved, locality-checked claurst `-m ollama/<tag>` (claurst-only;
    see `resolve_model_for_agent`); it reaches the agent command via `launch`."""
    with _capture_session(
        runner, target_abs, agent=agent, capture_dir=capture_dir, base_allow_hosts=allow_hosts
    ) as cap:
        _ensure_provisioned(
            runner,
            name,
            target_abs,
            agent=agent,
            allow_hosts=cap.allow_hosts,
            home=home,
            registry_path=registry_path,
        )
        launch(
            runner,
            name,
            target_abs,
            agent=agent,
            ollama_url=ollama_url,
            env_pairs=env_pairs,
            env_files=env_files,
            home=home,
            agent_args=agent_args,
            model=model,
            capture_relay_port=cap.relay_upstream_port,
        )


def exec_in_container(runner: Runner, name: str, command: str, *, why: str) -> list[str]:
    """Advise (and under --apply, run) a shell command inside the sandbox VM.

    Non-tty (`bash -lc`, no `-it`) so it works headless / under --apply. `exec`
    auto-starts a created-but-stopped sandbox, so this needs no explicit `start`.
    """
    return runner.advise(["docker", "sandbox", "exec", name, "bash", "-lc", command], why=why)


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
    agent: str = DEFAULT_AGENT,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
    env_pairs: list[str] | None = None,
    env_files: list[str] | None = None,
    home: Path | None = None,
    registry_path: Path | None = None,
    capture_dir: Path | None = None,
) -> list[str]:
    """Open an interactive bash shell inside the sandbox VM — `start` minus the agent
    launch.

    SYNC REQUIREMENT: see `start`. `shell` MUST stay identical to `start` except for
    the final in-container command: same provisioning gate (`_ensure_provisioned`),
    same `--capture` wrap (`_capture_session`), and same session setup (`_exec_session`:
    `-w <target>`, the env-file with agent auth / Ollama URL / relocated config home /
    resolved `{env:}` refs), so a tool you run by hand from this shell is wired exactly
    as `start` would wire the agent. The ONLY difference is the container command —
    `bash` instead of the agent binary."""
    with _capture_session(
        runner, target_abs, agent=agent, capture_dir=capture_dir, base_allow_hosts=allow_hosts
    ) as cap:
        _ensure_provisioned(
            runner,
            name,
            target_abs,
            agent=agent,
            allow_hosts=cap.allow_hosts,
            home=home,
            registry_path=registry_path,
        )
        return _exec_session(
            runner,
            name,
            target_abs,
            agent=agent,
            ollama_url=ollama_url,
            env_pairs=env_pairs or [],
            env_files=env_files or [],
            home=home,
            container_argv=["bash"],
            why=f"open a shell in sandbox '{name}'",
        )


def ls(registry_path: Path | None = None) -> None:
    """Read-only: print each recorded `name → target (agent)` and whether it is
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
        log_info(f"{name} → {info.get('target')} ({info.get('agent')}) [{status}]")


def rebuild(
    runner: Runner,
    name: str,
    target_abs: Path,
    *,
    agent: str = DEFAULT_AGENT,
    allow_hosts: tuple[str, ...] = DEFAULT_ALLOW_HOSTS,
    home: Path | None = None,
    registry_path: Path | None = None,
) -> list[list[str]]:
    """Recycle the sandbox: remove it (if present), then re-provision from scratch.

    `docker sandbox rm` takes no force flag and errors on a missing sandbox, so
    under --apply we stop-then-remove only when it actually exists. The agent home
    (c) lives on the host, so it survives this recycle.
    """
    cmds: list[list[str]] = []
    if not _live(runner) or sandbox_exists(name):
        # Stop first so rm doesn't trip on a running VM, then remove.
        cmds.append(stop(runner, name))
        cmds.append(
            runner.advise(["docker", "sandbox", "rm", name], why=f"remove sandbox '{name}'")
        )
    cmds += provision(
        runner,
        name,
        target_abs,
        agent=agent,
        allow_hosts=allow_hosts,
        home=home,
        registry_path=registry_path,
    )
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
