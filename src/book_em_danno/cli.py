"""The `danno` CLI. Three commands — `install`, `doctor`, `sandbox` — over the
two-tier automation policy: advise by default, execute under `--apply`, never
under `--dry-run`. `install` is the one provisioning path; `sandbox` operates the
provisioned VM; `doctor` is a read-only preflight."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from .commands import doctor as doctor_cmd
from .commands import install as install_cmd
from .commands import sandbox as sandbox_cmd
from .config.loader import DannoConfigError, load_config
from .config.schema import DannoConfig
from .core.exec import CommandNotFoundError, Runner, console, log_err


@dataclass
class State:
    config_path: Path = Path("danno.toml")
    apply: bool = False
    dry_run: bool = False
    verbose: bool = False

    def runner(self) -> Runner:
        return Runner(apply=self.apply, dry_run=self.dry_run, verbose=self.verbose)


state = State()

app = typer.Typer(
    no_args_is_help=True,
    help="Declarative, transparent OpenCode hybrid-runtime setup driven by danno.toml.",
)
sandbox_app = typer.Typer(no_args_is_help=True, help="Operate the provisioned Docker sandbox.")
app.add_typer(sandbox_app, name="sandbox")


@app.callback()
def main(
    config: Path = typer.Option(Path("danno.toml"), "--config", help="Path to danno.toml."),
    apply: bool = typer.Option(
        False, "--apply", help="Execute host/Docker/Ollama commands instead of only printing them."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Show intended actions and diffs without writing/executing."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug output."),
) -> None:
    state.config_path = config
    state.apply = apply
    state.dry_run = dry_run
    state.verbose = verbose


def _load() -> DannoConfig:
    try:
        return load_config(state.config_path)
    except DannoConfigError as exc:
        log_err(str(exc))
        raise typer.Exit(code=2) from exc


@app.command()
def install(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    ados_repo: str = typer.Option(
        None, "--ados-repo", help="ADOS checkout to install from (else auto-detected)."
    ),
) -> None:
    """Provision a target project: config + Ollama models + tools + sandbox.

    Default prints the host/Docker commands to run yourself; `--dry-run` previews
    everything (writing nothing); `--apply` executes. Stops before the TUI —
    launch with `danno sandbox start`.
    """
    cfg = _load()
    try:
        install_cmd.run_install(cfg, target, state.runner(), ados_repo=ados_repo)
    except (install_cmd.InstallError, NotImplementedError, ValueError) as exc:
        log_err(str(exc))
        raise typer.Exit(code=3) from exc
    except CommandNotFoundError as exc:
        log_err(str(exc))
        raise typer.Exit(code=4) from exc


@app.command()
def doctor() -> None:
    """Read-only preflight: report environment readiness with copy-paste fixes."""
    failed = doctor_cmd.run_doctor()
    if failed:
        raise typer.Exit(code=1)


def _sandbox_target(target: Path, name: str | None) -> tuple[Path, str]:
    abs_target = Path(target).resolve()
    if not abs_target.is_dir():
        log_err(f"target directory not found: {target}")
        raise typer.Exit(code=3)
    return abs_target, (name or sandbox_cmd.default_name(abs_target))


@sandbox_app.command("start")
def sandbox_start(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help="Sandbox name (default danno-<dir>)."),
    env: list[str] = typer.Option(None, "--env", help="KEY=VAL to inject (repeatable)."),
    env_file: list[str] = typer.Option(None, "--env-file", help="File of KEY=VAL to inject."),
) -> None:
    """Provision (if needed) and launch the in-container OpenCode TUI."""
    abs_target, sandbox_name = _sandbox_target(target, name)
    sandbox_cmd.start(
        state.runner(), sandbox_name, abs_target, env_pairs=env or [], env_files=env_file or []
    )


@sandbox_app.command("shell")
def sandbox_shell(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help="Sandbox name."),
) -> None:
    """Open an interactive bash shell inside the sandbox VM."""
    _, sandbox_name = _sandbox_target(target, name)
    sandbox_cmd.shell(state.runner(), sandbox_name)


@sandbox_app.command("stop")
def sandbox_stop(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help="Sandbox name."),
) -> None:
    """Stop the sandbox VM."""
    _, sandbox_name = _sandbox_target(target, name)
    sandbox_cmd.stop(state.runner(), sandbox_name)


@sandbox_app.command("rebuild")
def sandbox_rebuild(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help="Sandbox name."),
) -> None:
    """Remove and re-provision the sandbox from scratch."""
    abs_target, sandbox_name = _sandbox_target(target, name)
    sandbox_cmd.rebuild(state.runner(), sandbox_name, abs_target)


@sandbox_app.command("update")
def sandbox_update(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
    name: str = typer.Option(None, "--name", help="Sandbox name."),
) -> None:
    """Advise how to update OpenCode inside the sandbox."""
    _, sandbox_name = _sandbox_target(target, name)
    sandbox_cmd.update(state.runner(), sandbox_name)


# Re-exported so tests and `from book_em_danno.cli import console` keep working.
__all__ = ["app", "console", "state"]
