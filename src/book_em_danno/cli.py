"""The `danno` CLI. Three commands — `install`, `doctor`, `sandbox` — over the
two-mode automation policy: advise by default, execute under `--apply`. `install`
is the one provisioning path; `sandbox` operates the provisioned VM; `doctor` is a
read-only preflight. `--apply` is a per-command option (`danno install --apply`)."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

import typer

from .commands import doctor as doctor_cmd
from .commands import install as install_cmd
from .commands import sandbox as sandbox_cmd
from .config.loader import DannoConfigError, load_config
from .config.schema import DannoConfig
from .core import registry
from .core.exec import CommandFailedError, CommandNotFoundError, Runner, console, log_err

app = typer.Typer(
    no_args_is_help=True,
    help="Declarative, transparent OpenCode hybrid-runtime setup driven by danno.toml.",
)
sandbox_app = typer.Typer(no_args_is_help=True, help="Operate the provisioned Docker sandbox.")
app.add_typer(sandbox_app, name="sandbox")

# Per-command options shared across the side-effecting commands (mirrors _AGENT_OPT).
_APPLY_OPT = typer.Option(
    False, "--apply", help="Execute host/Docker/Ollama commands instead of only printing them."
)
_VERBOSE_OPT = typer.Option(False, "--verbose", "-v", help="Debug output.")
_CONFIG_OPT = typer.Option(Path("danno.toml"), "--config", help="Path to danno.toml.")


def _version_callback(value: bool) -> None:
    if not value:
        return
    try:
        ver = pkg_version("danno")
    except PackageNotFoundError:
        ver = "unknown (dev)"
    console.print(f"danno {ver}")
    raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the danno version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    pass


def _load(config_path: Path) -> DannoConfig:
    try:
        return load_config(config_path)
    except DannoConfigError as exc:
        log_err(str(exc))
        raise typer.Exit(code=2) from exc


def _guard(action: Callable[[], object]) -> None:
    """Run a Tier-2 action, turning a failed/missing external command into a clean
    exit 4 instead of a traceback."""
    try:
        action()
    except (CommandFailedError, CommandNotFoundError) as exc:
        log_err(str(exc))
        raise typer.Exit(code=4) from exc


@app.command()
def install(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    ados_repo: str = typer.Option(
        None, "--ados-repo", help="ADOS checkout to install from (else auto-detected)."
    ),
    config: Path = _CONFIG_OPT,
    apply: bool = _APPLY_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Provision a target project: config + Ollama models + tools + sandbox.

    Default prints the host/Docker commands to run yourself; `--apply` executes.
    Stops before the TUI — launch with `danno sandbox start`.
    """
    cfg = _load(config)
    runner = Runner(apply=apply, verbose=verbose)
    try:
        install_cmd.run_install(cfg, target, runner, ados_repo=ados_repo)
    except (install_cmd.InstallError, NotImplementedError, ValueError) as exc:
        log_err(str(exc))
        raise typer.Exit(code=3) from exc
    except (CommandNotFoundError, CommandFailedError) as exc:
        log_err(str(exc))
        raise typer.Exit(code=4) from exc


@app.command()
def doctor() -> None:
    """Read-only preflight: report environment readiness with copy-paste fixes."""
    failed = doctor_cmd.run_doctor()
    if failed:
        raise typer.Exit(code=1)


_AGENT_OPT = typer.Option(
    sandbox_cmd.DEFAULT_AGENT,
    "--agent",
    help="Docker prebuilt agent (opencode, claude, …); non-default agents get a separate sandbox.",
)


def _sandbox_target(target: Path, name: str | None, agent: str) -> tuple[Path, str]:
    abs_target = Path(target).resolve()
    if not abs_target.is_dir():
        log_err(f"target directory not found: {target}")
        raise typer.Exit(code=3)
    return abs_target, (name or sandbox_cmd.default_name(abs_target, agent))


def _resolve_home(abs_target: Path, sandbox_name: str) -> Path | None:
    """Resolve the agent-home dir (loud exit 2 on a malformed config)."""
    try:
        return sandbox_cmd.resolve_home(abs_target, sandbox_name)
    except DannoConfigError as exc:
        log_err(str(exc))
        raise typer.Exit(code=2) from exc


_NAME_OPT_HELP = "Sandbox name (default danno-<parent>-<dir>)."


@sandbox_app.command(
    "start",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def sandbox_start(
    ctx: typer.Context,
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help=_NAME_OPT_HELP),
    agent: str = _AGENT_OPT,
    env: list[str] = typer.Option(None, "--env", help="KEY=VAL to inject (repeatable)."),
    env_file: list[str] = typer.Option(None, "--env-file", help="File of KEY=VAL to inject."),
    apply: bool = _APPLY_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Launch the in-container agent (provisioning it first under `--apply`).

    Launching is the command's purpose, so it runs without `--apply`; `--apply`
    additionally executes the provisioning side effects (create/proxy). On an
    unprovisioned sandbox without `--apply`, it fails loud with the fix.

    Tip: `cd <project> && danno sandbox start` (no --target/--name) recomputes the
    same name every time — stand in the sandbox's directory rather than naming it.

    Anything after `--` is forwarded verbatim to the agent, e.g.
    `danno sandbox start --agent claude -- --resume <session-id>`.
    """
    abs_target, sandbox_name = _sandbox_target(target, name, agent)
    home = _resolve_home(abs_target, sandbox_name)
    _guard(
        lambda: sandbox_cmd.start(
            Runner(apply=apply, verbose=verbose),
            sandbox_name,
            abs_target,
            agent=agent,
            env_pairs=env or [],
            env_files=env_file or [],
            home=home,
            registry_path=registry.default_path(),
            agent_args=ctx.args,
        )
    )


@sandbox_app.command("shell")
def sandbox_shell(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help=_NAME_OPT_HELP),
    agent: str = _AGENT_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Open an interactive bash shell inside the sandbox VM."""
    _, sandbox_name = _sandbox_target(target, name, agent)
    _guard(lambda: sandbox_cmd.shell(Runner(verbose=verbose), sandbox_name))


@sandbox_app.command("stop")
def sandbox_stop(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help=_NAME_OPT_HELP),
    agent: str = _AGENT_OPT,
    apply: bool = _APPLY_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Stop the sandbox VM."""
    _, sandbox_name = _sandbox_target(target, name, agent)
    _guard(lambda: sandbox_cmd.stop(Runner(apply=apply, verbose=verbose), sandbox_name))


@sandbox_app.command("rebuild")
def sandbox_rebuild(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help=_NAME_OPT_HELP),
    agent: str = _AGENT_OPT,
    apply: bool = _APPLY_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Remove and re-provision the sandbox from scratch (the agent home survives)."""
    abs_target, sandbox_name = _sandbox_target(target, name, agent)
    home = _resolve_home(abs_target, sandbox_name)
    _guard(
        lambda: sandbox_cmd.rebuild(
            Runner(apply=apply, verbose=verbose),
            sandbox_name,
            abs_target,
            agent=agent,
            home=home,
            registry_path=registry.default_path(),
        )
    )


@sandbox_app.command("update")
def sandbox_update(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help=_NAME_OPT_HELP),
    agent: str = _AGENT_OPT,
    apply: bool = _APPLY_OPT,
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Advise how to update the agent inside the sandbox."""
    _, sandbox_name = _sandbox_target(target, name, agent)
    _guard(lambda: sandbox_cmd.update(Runner(apply=apply, verbose=verbose), sandbox_name, agent))


@sandbox_app.command("ls")
def sandbox_ls() -> None:
    """Read-only: list recorded sandboxes (name → target) and their live status."""
    sandbox_cmd.ls(registry.default_path())


# Re-exported so tests and `from book_em_danno.cli import console` keep working.
__all__ = ["app", "console"]
