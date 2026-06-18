"""Orchestrate a `danno validate` run: provision, sweep, baseline, report, menu.

This is the host-side driver the `danno validate` CLI command calls. It ties the
tested harness pieces together — `prepare_workspace` → `provision` → `run_sweep`
(+ optional `run_baseline`) → `write_sweep_report` / `write_menu` /
`write_results_json` → teardown — and routes per-tier progress to an injected
`Reporter` so the CLI can show live status. Design of record:
`.docs/ux-danno-validate-cli.md`.

`validate` runs immediately (like `sandbox start`), so the orchestrator always
executes (`Runner(apply=True)`); `--dry-run` resolves the plan and returns before
any side effect. The battery never runs in the user's project: it provisions
**disposable, validator-owned** sandboxes over a **throwaway workspace** seeded from
a copy of the project's `danno.toml`, and tears them down (unless `--keep-sandboxes`).

The heavy steps (`provision`, `run_sweep`, `run_baseline`, `prepare_workspace`) are
called as module attributes so tests can monkeypatch them and exercise the
orchestration logic without a Docker daemon.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

from book_em_danno.commands import sandbox as sb
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import Runner
from danno_validator.baseline import BASELINE_MODEL, run_baseline
from danno_validator.events import ValidateEvent
from danno_validator.matrix import model_variants
from danno_validator.menu import write_menu
from danno_validator.report import write_sweep_report
from danno_validator.serialize import run_record, write_results_json
from danno_validator.sweep import SweepResult, prepare_workspace, run_sweep

DEFAULT_AGENT = sb.DEFAULT_AGENT


@dataclass
class ValidateOptions:
    """Resolved `danno validate` flags (see the CLI / the UX doc for semantics)."""

    target: Path
    only: list[str] | None = None
    max_level: int = 2
    baseline: bool = False
    baseline_model: str | None = None
    agent: str = DEFAULT_AGENT
    workspace: Path | None = None
    out_dir: Path | None = None
    menu: bool = True
    menu_path: Path | None = None
    keep_sandboxes: bool = False
    reset: bool = True
    strict: bool = False
    dry_run: bool = False


@dataclass
class ValidatePlan:
    """The resolved plan — what a run *would* do. Printed by `--dry-run` and as the
    run's preamble."""

    config_path: Path
    declared_models: list[str]
    swept_models: list[str]
    max_level: int
    baseline: bool
    baseline_model: str | None
    agent: str
    workspace: Path
    out_dir: Path
    sweep_sandbox: str
    baseline_sandbox: str | None


@dataclass
class ValidateResult:
    """The outcome: the resolved plan, the matrix rows, and the written paths."""

    plan: ValidatePlan
    dry_run: bool
    results: list[SweepResult] = field(default_factory=list)
    index: Path | None = None
    menu_path: Path | None = None
    results_json: Path | None = None
    strict_failed: bool = False


class Reporter:
    """Sink for run progress. The base class is a no-op (library/tests); the CLI
    passes a `ConsoleReporter`. Methods are observational only."""

    def plan(self, plan: ValidatePlan, *, dry_run: bool) -> None: ...

    def phase(self, text: str) -> None: ...

    def event(self, ev: ValidateEvent) -> None: ...

    def summary(self, result: ValidateResult) -> None: ...


def _levels(max_level: int) -> tuple[bool, bool]:
    """(run_level1, run_level2) from the `--max-level` cap."""
    return max_level >= 1, max_level >= 2


def _validate_names(target: Path) -> tuple[str, str]:
    """The disposable sweep + baseline sandbox names for `target`.

    Derived from the project's normal sandbox base with a `-validate` infix, so they
    never collide with the user's real `danno-<parent>-<dir>` sandboxes.
    """
    base = sb.default_name(target.resolve(), sb.DEFAULT_AGENT)
    return f"{base}-validate", f"{base}-validate-claude"


def _config_ok(s: SweepResult, max_level: int) -> bool:
    """True iff a swept config cleared every *requested* tier (for `--strict`)."""
    if not s.result.passed:
        return False
    if max_level >= 1 and not (s.level1 is not None and s.level1.passed):
        return False
    if max_level >= 2 and not (s.level2 is not None and s.level2.passed):
        return False
    return True


def _resolve_plan(config: DannoConfig, opts: ValidateOptions, *, timestamp: str) -> ValidatePlan:
    """Resolve every defaulted path/name and validate `--only` (fails loud on an
    undeclared model, before any sandbox is created)."""
    target = opts.target.resolve()
    swept = [v.model_name for v in model_variants(config, only=opts.only)]
    sweep_name, baseline_name = _validate_names(target)
    workspace = (opts.workspace or Path(tempfile.gettempdir()) / sweep_name).resolve()
    out_dir = (opts.out_dir or Path(".danno-validator") / timestamp).resolve()
    return ValidatePlan(
        config_path=(target / "danno.toml"),
        declared_models=sorted(config.models),
        swept_models=swept,
        max_level=opts.max_level,
        baseline=opts.baseline,
        baseline_model=opts.baseline_model,
        agent=opts.agent,
        workspace=workspace,
        out_dir=out_dir,
        sweep_sandbox=sweep_name,
        baseline_sandbox=baseline_name if opts.baseline else None,
    )


def _teardown(runner: Runner, name: str) -> None:
    """Stop and remove a disposable validator sandbox (best effort under --apply)."""
    sb.stop(runner, name)
    runner.advise(["docker", "sandbox", "rm", name], why=f"remove validator sandbox '{name}'")


def run_validate(
    config: DannoConfig,
    opts: ValidateOptions,
    runner: Runner,
    *,
    reporter: Reporter | None = None,
    now: datetime | None = None,
    version: str | None = None,
) -> ValidateResult:
    """Execute a validate run end to end and return its `ValidateResult`.

    `reporter` receives the plan, phase headers, per-tier `ValidateEvent`s, and the
    final summary (defaults to the no-op base). `now`/`version` are injected for
    deterministic tests; they stamp the run directory and `results.json`.
    """
    reporter = reporter or Reporter()
    now = now or datetime.now(UTC)
    version = version or _danno_version()
    timestamp = now.strftime("%Y-%m-%dT%H-%M-%S")

    plan = _resolve_plan(config, opts, timestamp=timestamp)
    reporter.plan(plan, dry_run=opts.dry_run)
    if opts.baseline:
        # Fail loud *before* provisioning anything if claude auth is missing.
        sb.agent_env("claude", sb.DEFAULT_OLLAMA_URL)
    if opts.dry_run:
        return ValidateResult(plan=plan, dry_run=True)

    level1, level2 = _levels(opts.max_level)

    reporter.phase(f"prepare workspace  {plan.workspace}")
    prepare_workspace(runner, plan.workspace, config)

    reporter.phase(f"provision {opts.agent} sandbox  {plan.sweep_sandbox}")
    sb.provision(runner, plan.sweep_sandbox, plan.workspace, agent=opts.agent, registry_path=None)
    results = run_sweep(
        runner,
        plan.sweep_sandbox,
        config=config,
        workspace_root=plan.workspace,
        only=opts.only,
        agent=opts.agent,
        reset=opts.reset,
        level1=level1,
        level2=level2,
        on_event=reporter.event,
    )

    if opts.baseline and plan.baseline_sandbox is not None:
        reporter.phase(f"provision claude sandbox  {plan.baseline_sandbox}")
        sb.provision(
            runner, plan.baseline_sandbox, plan.workspace, agent="claude", registry_path=None
        )
        results.append(
            run_baseline(
                runner,
                plan.baseline_sandbox,
                workspace_root=plan.workspace,
                model=opts.baseline_model,
                reset=opts.reset,
                level1=level1,
                level2=level2,
                on_event=reporter.event,
            )
        )

    _, index = write_sweep_report(results, plan.out_dir)
    menu_path: Path | None = None
    if opts.menu:
        menu_path = write_menu(
            config,
            results,
            opts.menu_path or plan.out_dir / "menu.danno.toml",
            verified=now.strftime("%Y-%m-%d"),
        )
    record = run_record(
        results,
        config_path=plan.config_path,
        declared_models=plan.declared_models,
        run_meta=_run_meta(plan, opts),
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        danno_version=version,
        requested_baseline_model=opts.baseline_model,
    )
    results_json = write_results_json(record, plan.out_dir / "results.json")

    if not opts.keep_sandboxes:
        reporter.phase("tear down sandboxes")
        _teardown(runner, plan.sweep_sandbox)
        if opts.baseline and plan.baseline_sandbox is not None:
            _teardown(runner, plan.baseline_sandbox)

    swept = [s for s in results if s.variant.model_name != BASELINE_MODEL]
    strict_failed = opts.strict and any(not _config_ok(s, opts.max_level) for s in swept)
    out = ValidateResult(
        plan=plan,
        dry_run=False,
        results=results,
        index=index,
        menu_path=menu_path,
        results_json=results_json,
        strict_failed=strict_failed,
    )
    reporter.summary(out)
    return out


def _run_meta(plan: ValidatePlan, opts: ValidateOptions) -> dict[str, object]:
    """The `run` block recorded verbatim in results.json (what was asked for)."""
    return {
        "swept_models": plan.swept_models,
        "max_level": opts.max_level,
        "trials": 1,
        "reset": opts.reset,
        "agent": opts.agent,
        "workspace": str(plan.workspace),
        "out_dir": str(plan.out_dir),
        "sandboxes": {"sweep": plan.sweep_sandbox, "baseline": plan.baseline_sandbox},
        "baseline": {"enabled": opts.baseline, "requested_model": opts.baseline_model},
    }


def _danno_version() -> str:
    try:
        return pkg_version("danno")
    except PackageNotFoundError:
        return "unknown (dev)"
