"""The one provisioning path (`danno install`).

Chains the full happy path in order — validate → config → ollama → tools →
sandbox-create — each step honoring the two-tier policy, then prints the launch
hint. Stops before launching the TUI (that's `danno sandbox start`).
"""

from __future__ import annotations

from pathlib import Path

from ..config.generate import Action, generate
from ..config.loader import DannoConfigError
from ..config.schema import DannoConfig, OllamaBackend
from ..core import registry
from ..core.exec import Runner, log_err, log_info
from . import ollama, sandbox, tools


class InstallError(Exception):
    """Provisioning could not proceed (bad target, bad config)."""


def _resolve_target(target: Path) -> Path:
    abs_target = Path(target).resolve()
    if not abs_target.is_dir():
        raise InstallError(f"target directory not found: {target}")
    return abs_target


def _emit_config(cfg: DannoConfig, target_abs: Path, runner: Runner) -> None:
    """Tier-1: write/diff .opencode/opencode.jsonc (we own this file)."""
    result = generate(cfg, target_abs, apply=runner.apply)
    if result.action is Action.WROTE:
        log_info(f"[green]wrote[/green] {result.path}")
    elif result.action is Action.UNCHANGED:
        log_info(f"config unchanged: {result.path}")
    else:  # DIFF
        from ..core.exec import console

        console.print(result.diff or result.content)
        log_info(
            "[yellow]config differs from the existing file[/yellow]; "
            "re-run with --apply to write it."
        )


def _ollama_tags(cfg: DannoConfig) -> list[str]:
    """Unique Ollama model tags referenced by the agent map, in stable order."""
    tags: list[str] = []
    for model_name in sorted(set(cfg.agents.values())):
        model = cfg.models[model_name]
        backend = cfg.backends[model.backend]
        if isinstance(backend, OllamaBackend) and model.tag and model.tag not in tags:
            tags.append(model.tag)
    return tags


def run_install(
    cfg: DannoConfig,
    target: Path,
    runner: Runner,
    *,
    ados_repo: str | None = None,
) -> None:
    """Run the full provisioning happy path. Validation already happened in the
    loader; this orchestrates config, models, tools, and sandbox creation."""
    target_abs = _resolve_target(target)
    name = sandbox.default_name(target_abs)
    # Resolve the agent home the same way `sandbox start` does, so install creates
    # the sandbox with the home mount already in place — otherwise a later `start`
    # finds the sandbox existing, skips create, and the home is never mounted.
    try:
        home = sandbox.resolve_home(target_abs, name)
    except DannoConfigError as exc:
        raise InstallError(str(exc)) from exc

    log_info(f"provisioning {target_abs}")
    log_info("step 1/5 — config")
    _emit_config(cfg, target_abs, runner)

    log_info("step 2/5 — Ollama models")
    for tag in _ollama_tags(cfg):
        ollama.ensure_model(runner, tag)

    log_info("step 3/5 — tools")
    for tool in cfg.tools:
        try:
            tools.install_tool(runner, tool, target_abs, ados_repo=ados_repo)
        except tools.ToolInstallError as exc:
            log_err(f"tool '{tool.name}': {exc}")

    log_info("step 4/5 — sandbox")
    sandbox.provision(runner, name, target_abs, home=home, registry_path=registry.default_path())
    # OpenCode npm plugins install themselves from the generated opencode.jsonc;
    # only their optional in-container `setup` steps need an exec, post-create.
    sandbox.run_npm_setup(runner, name, cfg.npm)

    log_info("step 5/5 — ready")
    log_info(f"[green]ready[/green] — launch with: danno sandbox start --target {target}")
