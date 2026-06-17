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
import subprocess
import tempfile
import tomllib
from pathlib import Path

from pydantic import ValidationError

from ..config.loader import DannoConfigError
from ..config.schema import NpmPlugin, Sandbox
from ..core import registry
from ..core.exec import CommandFailedError, Runner, log_info, log_warn
from . import ollama

DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434/v1"
DEFAULT_ALLOW_HOSTS = ("localhost:11434",)
DEFAULT_AGENT = "opencode"
# Auth env vars Claude Code accepts, in preference order (subscription token first).
CLAUDE_AUTH_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


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

    cmd = ["docker", "sandbox", "create", "--name", name, agent, str(target_abs)]
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
    cmds = [
        create(runner, name, target_abs, agent, home=home, registry_path=registry_path),
        configure_proxy(runner, name, allow_hosts),
        # The network policy only takes on a fresh VM start; stop so the next
        # `start` applies the allow-rule.
        stop(runner, name),
    ]
    return cmds


def agent_env(agent: str, ollama_url: str, home: Path | None = None) -> list[str]:
    """The agent-specific `KEY=VAL` env-file lines.

    opencode reaches host Ollama via OLLAMA_BASE_URL. claude needs auth: prefer the
    subscription token (CLAUDE_CODE_OAUTH_TOKEN), else ANTHROPIC_API_KEY, read from
    danno's host environment. Fail loud (Working Rule 8) when neither is set. The
    secret only ever lands in the chmod-600 env-file, never on the command line.

    When `home` is set, the agent's global config is relocated onto the mounted
    host dir: claude via CLAUDE_CONFIG_DIR; opencode via XDG_CONFIG_HOME.

    opencode's data dir (XDG_DATA_HOME — its sqlite session store) is deliberately
    NOT relocated onto the mounted home: that mount is virtiofs, which can't honor
    `PRAGMA journal_mode = WAL`, so opencode crashes with a Drizzle error on start.
    Left unset, it defaults to the container's VM-local ext4 (~/.local/share),
    where WAL works. Tradeoff: sessions persist across stop/start but reset on a
    sandbox rebuild/reset (`docker sandbox` has no volume mount to do better).
    """
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
) -> list[str]:
    """Launch the in-container agent in the mounted repo (`-w <target>`), wired to
    host Ollama / agent auth via an env-file. Launching is the command's purpose, so
    it always executes (not gated by `--apply`). With a persistent `home`, the
    agent's config/history is relocated onto it; for claude, onboarding and
    workspace trust are pre-seeded so neither wizard nor the trust dialog blocks the
    launch. The exec runs with `check=False`: quitting the TUI is not a danno error."""
    env_pairs = env_pairs or []
    env_files = env_files or []
    if agent == "claude" and home is not None:
        seed_onboarding(home, target_abs)
    lines = agent_env(agent, ollama_url, home)
    injected = ", ".join(line.split("=", 1)[0] for line in lines)
    log_info(f"injecting {injected} via a chmod-600 --env-file")
    env_path = _build_env_file(lines, env_pairs, env_files)
    try:
        return runner.run(
            [
                "docker",
                "sandbox",
                "exec",
                "-it",
                "-w",
                str(target_abs),
                "--env-file",
                str(env_path),
                name,
                agent,
            ],
            why=f"launch {agent} in sandbox '{name}'",
            check=False,
        )
    finally:
        env_path.unlink(missing_ok=True)


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
) -> None:
    """Launch the in-container agent. Under `--apply`, provision (idempotent) first;
    otherwise just launch the already-provisioned sandbox, failing loud if it is
    missing rather than letting `docker sandbox exec` error on a missing sandbox."""
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
    launch(
        runner,
        name,
        target_abs,
        agent=agent,
        ollama_url=ollama_url,
        env_pairs=env_pairs,
        env_files=env_files,
        home=home,
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


def shell(runner: Runner, name: str) -> list[str]:
    """Open an interactive bash shell inside the sandbox VM (always executes — the
    interactive shell is the command's purpose, not a gated side effect). Runs with
    `check=False` so exiting the shell (non-zero status) is not a danno error."""
    return runner.run(
        ["docker", "sandbox", "exec", "-it", name, "bash"],
        why=f"open a shell in sandbox '{name}'",
        check=False,
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
