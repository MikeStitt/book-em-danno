"""The `danno` CLI. Commands — `install`, `doctor`, `sandbox`, `validate` — over the
two-mode automation policy: advise by default, execute under `--apply`. `install`
is the one provisioning path; `sandbox` operates the provisioned VM; `doctor` is a
read-only preflight; `validate` sweeps danno.toml's models through the tiered
battery (it runs immediately, like `sandbox start`). `--apply` is a per-command
option (`danno install --apply`)."""

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


@app.command()
def validate(
    target: Path = typer.Option(
        Path("."), "--target", "-C", help="Project whose danno.toml is swept."
    ),
    only: list[str] = typer.Option(
        None, "--only", help="Restrict the sweep to these danno.toml model keys (repeatable)."
    ),
    max_level: int = typer.Option(
        2, "--max-level", min=0, max=2, help="Highest tier (0 liveness · 1 +tool/bash · 2 +dev)."
    ),
    baseline: bool = typer.Option(
        False, "--baseline", help="Also run the Claude Code baseline row (needs a host token)."
    ),
    baseline_model: str = typer.Option(
        None,
        "--baseline-model",
        help="Pin the baseline's claude model (opus/sonnet/… or a full id).",
    ),
    judge: bool = typer.Option(
        False, "--judge", help="Grade L2 dev quality with an Anthropic judge (needs an API key)."
    ),
    judge_model: str = typer.Option(
        None, "--judge-model", help="Pin the judge model (opus/sonnet/haiku or a full id)."
    ),
    agent: str = typer.Option(
        sandbox_cmd.DEFAULT_AGENT,
        "--agent",
        help="Agent-under-test for the sweep (default opencode).",
    ),
    env: list[str] = typer.Option(
        None, "--env", help="KEY=VAL credential to inject into cloud-config sweeps (repeatable)."
    ),
    env_file: list[str] = typer.Option(
        None, "--env-file", help="File of KEY=VAL credentials to inject (repeatable)."
    ),
    workspace: Path = typer.Option(
        None, "--workspace", help="Throwaway workspace mount (default a temp dir)."
    ),
    out: Path = typer.Option(
        None, "--out", help="Report output dir (default .danno-validator/<timestamp>/)."
    ),
    menu: bool = typer.Option(
        True, "--menu/--no-menu", help="Emit the annotated menu danno.toml into the run dir."
    ),
    html: bool = typer.Option(
        False, "--html", help="Render the report to HTML (deferred — see help)."
    ),
    keep_sandboxes: bool = typer.Option(
        False, "--keep-sandboxes", help="Leave the disposable sandboxes up for debugging."
    ),
    reset: bool = typer.Option(
        True, "--reset/--no-reset", help="Guarded per-config workspace reset between configs."
    ),
    strict: bool = typer.Option(
        False, "--strict", help="Exit non-zero if any swept config fails its requested tiers."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the plan and exit without provisioning or running."
    ),
    verbose: bool = _VERBOSE_OPT,
) -> None:
    """Sweep danno.toml's models through the tiered battery and write the report.

    Runs immediately (like `sandbox start`), over disposable, validator-owned
    sandboxes seeded from a copy of your danno.toml — your project is never
    modified. `--dry-run` previews the plan. `--baseline` adds a Claude Code
    reference row (needs CLAUDE_CODE_OAUTH_TOKEN/ANTHROPIC_API_KEY). Outputs land
    under `.danno-validator/<timestamp>/` (report + menu + results.json).

    Cloud configs (an anthropic/NVIDIA/… model) need credentials to clear L0:
    danno auto-injects host-exported keys it can identify (the provider's
    `<PROVIDER>_API_KEY`, e.g. `ANTHROPIC_API_KEY`, and any `{env:VAR}` the config
    references); pass `--env KEY=VAL` / `--env-file` to supply or override. A
    missing key only warns — that config errors loudly in its own row. Local Ollama
    models need none.

    `--judge` adds a host-side Anthropic judge that grades L2 software-dev *quality*
    (clarity, over-/under-build) on top of the objective hidden-test verdict; it
    never changes pass/fail. Needs `ANTHROPIC_API_KEY` (API billing) and the
    `danno[validator]` extra; `--judge-model` pins the model (default opus). The
    graded verdict lands in the report and results.json.
    """
    from danno_validator.console import ConsoleReporter
    from danno_validator.run import ValidateOptions, run_validate

    if html:
        log_err(
            "--html is not yet wired: HTML rendering ships with the danno[validator] "
            "Sphinx extra (tracked for M7). Re-run without --html; the MyST report is "
            "still written to the run dir."
        )
        raise typer.Exit(code=3)

    cfg = _load(target / "danno.toml")
    opts = ValidateOptions(
        target=target,
        only=only or None,
        max_level=max_level,
        baseline=baseline,
        baseline_model=baseline_model,
        judge=judge,
        judge_model=judge_model,
        agent=agent,
        env=env or [],
        env_file=env_file or [],
        workspace=workspace,
        out_dir=out,
        menu=menu,
        keep_sandboxes=keep_sandboxes,
        reset=reset,
        strict=strict,
        dry_run=dry_run,
    )
    try:
        result = run_validate(
            cfg, opts, Runner(apply=True, verbose=verbose), reporter=ConsoleReporter()
        )
    except ValueError as exc:  # e.g. --only names an undeclared model (fail loud)
        log_err(str(exc))
        raise typer.Exit(code=3) from exc
    except (CommandFailedError, CommandNotFoundError) as exc:  # missing token / Docker
        log_err(str(exc))
        raise typer.Exit(code=4) from exc
    if result.strict_failed:
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
