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

import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

from book_em_danno.capture.wiring import (
    CaptureTarget,
    capture_allow_hosts,
    captures_running,
    plan_capture,
    uncaptured_cloud_refs,
)
from book_em_danno.commands import sandbox as sb
from book_em_danno.commands import sandbox_cli
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import CommandFailedError, Runner, log_info, log_warn
from danno_validator import harnesses
from danno_validator.baseline import BASELINE_MODEL, run_baseline
from danno_validator.events import ValidateEvent
from danno_validator.judge import JudgeFn
from danno_validator.matrix import model_variants
from danno_validator.menu import write_menu
from danno_validator.report import write_sweep_report
from danno_validator.serialize import run_record, write_results_json
from danno_validator.sweep import SweepResult, prepare_workspace, run_sweep

DEFAULT_HARNESS = sb.DEFAULT_HARNESS


@dataclass
class ValidateOptions:
    """Resolved `danno validate` flags (see the CLI / the UX doc for semantics)."""

    target: Path
    only: list[str] | None = None
    max_level: int = 2
    baseline: bool = False
    baseline_model: str | None = None
    judge: bool = False
    judge_model: str | None = None
    harness: str = DEFAULT_HARNESS
    env: list[str] = field(default_factory=list)
    env_file: list[str] = field(default_factory=list)
    workspace: Path | None = None
    out_dir: Path | None = None
    menu: bool = True
    menu_path: Path | None = None
    keep_sandboxes: bool = False
    reset: bool = True
    strict: bool = False
    dry_run: bool = False
    capture: bool = False
    capture_dir: Path | None = None


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
    judge: bool
    judge_model: str | None
    harness: str
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
    base = sb.default_name(target.resolve(), sb.DEFAULT_HARNESS)
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
        judge=opts.judge,
        judge_model=opts.judge_model,
        harness=opts.harness,
        workspace=workspace,
        out_dir=out_dir,
        sweep_sandbox=sweep_name,
        baseline_sandbox=baseline_name if opts.baseline else None,
    )


def _build_sweep_env_file(
    config: DannoConfig, opts: ValidateOptions, workspace: Path
) -> Path | None:
    """Build the chmod-600 credentials file bound into every opencode sweep exec.

    Combines `--env`/`--env-file` with host-exported values for every `{env:}` ref
    the swept config declares in opencode.jsonc (e.g. an openai-compatible backend's
    `api_key_env`). Missing keys only **warn** (the affected config errors loudly at
    L0 in its own row, which is informative) rather than aborting the whole sweep,
    since a run may legitimately target only the no-auth local models. Returns the
    file path, or `None` when nothing needs injecting (the local-only case — the
    sweep then runs exactly as before)."""
    augmented, missing = sb.resolve_env_refs(workspace, opts.env, opts.env_file)
    if missing:
        log_warn(
            f"no credentials for {', '.join(missing)} — those configs will error at "
            f"L0. Export them or pass `--env KEY=VAL` to inject them into the sweep."
        )
    # Fold danno.toml [env] into the sweep credentials file. The opencode.jsonc
    # {env:} refs (augmented) + --env-file ride the CLI tier (highest, as before);
    # [env] composes underneath. None only when truly nothing needs injecting.
    lines = sb.assemble_harness_env(
        config, harness_defaults=[], env_pairs=augmented, env_files=opts.env_file
    )
    if not lines:
        return None
    return sb._build_env_file(lines, [], [])


def _build_judge(judge: bool, judge_model: str | None = None) -> JudgeFn | None:
    """Build the L2 dev-quality `JudgeFn` from `--judge`, or `None` when off.

    Fails loud up front (Working Rule 8), *before* any sandbox is provisioned: a
    missing Anthropic key or the un-installed `danno[validator]` extra aborts here
    rather than mid-sweep. The judge runs host-side (not in the sandbox), so it
    needs the key in danno's own environment. It uses the Anthropic SDK, which
    honours `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` — *not* the Claude Code
    subscription `CLAUDE_CODE_OAUTH_TOKEN` (that authenticates Claude Code, not the
    SDK; see the baseline, which is the one place that token is used)."""
    if not judge:
        return None
    from danno_validator.judge import (
        DEFAULT_JUDGE_MODEL,
        AnthropicJudgeClient,
        JudgeError,
        make_judge,
    )

    if not any(os.environ.get(var) for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")):
        raise CommandFailedError(
            "the L2 dev-quality judge (--judge) needs an Anthropic API key, but neither "
            "ANTHROPIC_API_KEY nor ANTHROPIC_AUTH_TOKEN is set in danno's environment. "
            "Export one (API billing — the judge uses the Anthropic SDK, not the Claude "
            "Code subscription token), or drop --judge."
        )
    try:
        client = AnthropicJudgeClient()
    except JudgeError as exc:  # the `danno[validator]` extra (anthropic SDK) isn't installed
        raise CommandFailedError(str(exc)) from exc
    return make_judge(client, model=judge_model or DEFAULT_JUDGE_MODEL)


def _setup_capture(
    config: DannoConfig, opts: ValidateOptions, plan: ValidatePlan
) -> tuple[DannoConfig, list[CaptureTarget], tuple[str, ...]]:
    """Resolve `--capture`: (config to generate from, proxy targets, sandbox allow-list).

    Off → the original config, no targets, the default egress allow-list. On → rewrite
    each redirectable backend's base_url at a proxy (`plan_capture`) and open its port,
    warning loud (Working Rule 8) about traffic capture cannot reach: built-in cloud
    refs (`anthropic/*`, no base_url lever) and the Claude Code baseline."""
    if not opts.capture:
        return config, [], sb.DEFAULT_ALLOW_HOSTS
    capture_dir = opts.capture_dir or (plan.out_dir / "captures")
    cfg_for_run, targets = plan_capture(config, capture_dir)
    log_info(f"--capture: recording opencode<->backend wire traffic to {capture_dir}")
    uncap = uncaptured_cloud_refs(config)
    if uncap:
        log_warn(
            "--capture cannot record built-in cloud refs (no danno base_url lever): "
            f"{', '.join(uncap)}"
        )
    if opts.baseline:
        log_warn("--capture does not record the Claude Code baseline (api.anthropic.com).")
    return cfg_for_run, targets, capture_allow_hosts(targets, sb.DEFAULT_ALLOW_HOSTS)


def _teardown(runner: Runner, name: str) -> None:
    """Stop and remove a disposable validator sandbox (best effort under --apply)."""
    sb.stop(runner, name)
    runner.advise(sandbox_cli.rm_argv(name), why=f"remove validator sandbox '{name}'")


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
        sb.harness_env("claude", sb.DEFAULT_OLLAMA_URL)
    # Same discipline for the judge: validate its auth/SDK up front (also on a dry run,
    # so the preview surfaces a misconfigured --judge before an expensive real run).
    judge = _build_judge(opts.judge, opts.judge_model)
    if opts.dry_run:
        return ValidateResult(plan=plan, dry_run=True)

    level1, level2 = _levels(opts.max_level)

    # `--capture`: rewrite redirectable backends' base_urls at recording proxies and
    # open their ports in the sandbox egress. Off → original config + default egress.
    cfg_for_run, capture_targets, allow_hosts = _setup_capture(config, opts, plan)

    reporter.phase(f"prepare workspace  {plan.workspace}")
    prepare_workspace(runner, plan.workspace, cfg_for_run)

    # The proxies must be up for every harness request; start them before provisioning
    # and tear them down after the last turn (a no-op context when capture is off).
    with captures_running(capture_targets):
        # `opts.harness` selects the harness-under-test for the sweep — always a dialer
        # (claude is the separate `--baseline` reference, provisioned below). The
        # registry owns each dialer's sandbox image and post-provision install: opencode
        # is a prebuilt image with nothing to install; claurst runs in a `shell` VM
        # and installs post-provision. opencode is driven by run_sweep's built-in
        # read-write run-agent ("build", the per-level default), so it injects no
        # `make_run_turn`; other dialers inject their registry `TurnFn` factory.
        harness = harnesses.get(opts.harness)
        reporter.phase(f"provision {opts.harness} sandbox  {plan.sweep_sandbox}")
        sb.provision(
            runner,
            plan.sweep_sandbox,
            plan.workspace,
            harness=harness.sandbox_image,
            allow_hosts=allow_hosts,
            registry_path=None,
        )
        if opts.harness != DEFAULT_HARNESS:
            reporter.phase(f"install {opts.harness}  {plan.sweep_sandbox}")
            harness.install(runner, plan.sweep_sandbox, config)
        # Credentials for swept cloud configs: bound into every harness exec via
        # --env-file, removed after the sweep (the secret never lingers on disk).
        sweep_env_file = _build_sweep_env_file(config, opts, plan.workspace)
        make_run_turn = None if opts.harness == DEFAULT_HARNESS else harness.turn_fn
        try:
            results = run_sweep(
                runner,
                plan.sweep_sandbox,
                config=cfg_for_run,
                workspace_root=plan.workspace,
                only=opts.only,
                reset=opts.reset,
                level1=level1,
                level2=level2,
                env_file=sweep_env_file,
                make_run_turn=make_run_turn,
                judge=judge,
                on_event=reporter.event,
            )
        finally:
            if sweep_env_file is not None:
                sweep_env_file.unlink(missing_ok=True)

        if opts.baseline and plan.baseline_sandbox is not None:
            reporter.phase(f"provision claude sandbox  {plan.baseline_sandbox}")
            sb.provision(
                runner,
                plan.baseline_sandbox,
                plan.workspace,
                harness="claude",
                allow_hosts=allow_hosts,
                registry_path=None,
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
                    judge=judge,
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
        "harness": opts.harness,
        "workspace": str(plan.workspace),
        "out_dir": str(plan.out_dir),
        "sandboxes": {"sweep": plan.sweep_sandbox, "baseline": plan.baseline_sandbox},
        "baseline": {"enabled": opts.baseline, "requested_model": opts.baseline_model},
        "judge": {"enabled": opts.judge, "requested_model": opts.judge_model},
        "capture": {
            "enabled": opts.capture,
            "dir": str(opts.capture_dir or (plan.out_dir / "captures")) if opts.capture else None,
        },
    }


def _danno_version() -> str:
    try:
        return pkg_version("danno")
    except PackageNotFoundError:
        return "unknown (dev)"
