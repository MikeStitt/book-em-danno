"""The `danno` CLI. Phase 1 scaffold: config generation is real; host/Docker
commands are stubs that honor the two-tier automation policy (advise by default,
execute only under --apply)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console

from .config.generate import Action, generate
from .config.loader import DannoConfigError, load_config
from .config.schema import DannoConfig

console = Console()


@dataclass
class State:
    config_path: Path = Path("danno.toml")
    apply: bool = False
    dry_run: bool = False
    verbose: bool = False


state = State()

app = typer.Typer(
    no_args_is_help=True,
    help="Declarative, transparent OpenCode hybrid-runtime setup driven by danno.toml.",
)
config_app = typer.Typer(no_args_is_help=True, help="Generate the OpenCode config from danno.toml.")
tools_app = typer.Typer(no_args_is_help=True, help="List or install the agentic tool catalog.")
app.add_typer(config_app, name="config")
app.add_typer(tools_app, name="tools")


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
        console.print(f"[red][ERROR][/red] {exc}")
        raise typer.Exit(code=2) from exc


def _run_generate(target: Path) -> None:
    cfg = _load()
    try:
        result = generate(cfg, target, apply=state.apply, dry_run=state.dry_run)
    except (NotImplementedError, ValueError) as exc:
        console.print(f"[red][ERROR][/red] {exc}")
        raise typer.Exit(code=3) from exc

    if result.action is Action.WROTE:
        console.print(f"[green][SUCCESS][/green] wrote {result.path}")
    elif result.action is Action.UNCHANGED:
        console.print(f"[INFO] config unchanged: {result.path}")
    else:  # DIFF
        console.print(result.diff or result.content)
        console.print(
            "[yellow][INFO][/yellow] differs from the existing config; "
            "re-run with --apply to write it."
        )


@app.command()
def install(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
) -> None:
    """Read danno.toml and write the OpenCode config into a target project."""
    _run_generate(target)


@config_app.command("generate")
def config_generate(
    target: Path = typer.Option(Path("."), "--target", help="Target project."),
) -> None:
    """Generate .opencode/opencode.jsonc from danno.toml."""
    _run_generate(target)


@tools_app.command("list")
def tools_list() -> None:
    """List the agentic tool catalog declared in danno.toml."""
    cfg = _load()
    if not cfg.tools:
        console.print("[INFO] no tools declared in danno.toml")
        return
    for tool in cfg.tools:
        console.print(f"  {tool.name:20} {tool.install_to:8} {tool.source}")


@tools_app.command("install")
def tools_install() -> None:
    """Show (or with --apply, run) the per-tool install commands."""
    cfg = _load()
    for tool in cfg.tools:
        console.print(f"[INFO] {tool.name} -> {tool.install_to}: install from {tool.source}")
    console.print(
        "[yellow][INFO][/yellow] tool install is advisory; "
        "execution with --apply is not yet implemented (Phase 2)."
    )


@app.command()
def doctor() -> None:
    """Diagnostics: report environment readiness with copy-paste fixes."""
    console.print("[INFO] doctor is not yet implemented in the Python port (Phase 2).")


@app.command()
def ollama(action: str = typer.Argument(..., help="e.g. 'pull gemma3:27b'")) -> None:
    """Advise how to manage the Ollama server and models (run with --apply)."""
    console.print(f"[INFO] would manage Ollama: {action} (Phase 2; advisory by default).")


@app.command()
def sandbox(action: str = typer.Argument(..., help="start | shell | config")) -> None:
    """Advise how to create/operate the Docker sandbox (run with --apply)."""
    console.print(f"[INFO] would run sandbox '{action}' (Phase 2; copy-paste by default).")
