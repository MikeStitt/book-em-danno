"""Benchmark whole candidate configs for *editing performance* (Phase 2).

`danno validate` answers "which declared model works"; `danno benchmark` answers
"which whole agent CONFIG edits best". Where the sweep varies one axis (model, via
`-m`) over the project's own `danno.toml`, the benchmark varies the **entire
opencode config**: each candidate is a directory holding a real `.opencode/` tree
(opencode.jsonc + agent `.md`), and the benchmark applies each into the throwaway,
validator-owned workspace and runs the *same* tiered battery (L0→L1→L2 + the
optional dev-quality judge) the validator uses, plus the Claude Code baseline.

Reuse over reinvention: candidate runs go through `sweep.run_tiers` (the shared
L0→L1→L2 short-circuit) with an empty `model_ref` so no `-m` is passed — each
candidate's own opencode.jsonc carries the model. The report and results.json are
the validator's (`report.write_sweep_report`, `serialize`), so a benchmark row reads
on the same editing-performance axes (L2 hidden-test pass + judge clarity/sizing +
tokens/latency) and against the same baseline. The user's project is never touched —
only the disposable workspace is, guarded by the `.danno-validator-workspace` marker.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from book_em_danno.commands import sandbox as sb
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import Runner, log_info, log_warn
from danno_validator.baseline import run_baseline
from danno_validator.driver import reset_workspace
from danno_validator.matrix import ConfigVariant
from danno_validator.report import write_sweep_report
from danno_validator.run import (
    _build_judge,
    _config_ok,
    _danno_version,
    _levels,
    _teardown,
)
from danno_validator.serialize import run_record, write_results_json
from danno_validator.sweep import SweepResult, _authed_opencode_run, prepare_workspace, run_tiers

_OPENCODE_DIR = ".opencode"


@dataclass
class BenchmarkOptions:
    """Resolved `danno benchmark` flags (a focused subset of `validate`'s)."""

    configs_dir: Path
    target: Path
    max_level: int = 2
    baseline: bool = False
    baseline_model: str | None = None
    judge: bool = False
    judge_model: str | None = None
    agent: str = sb.DEFAULT_AGENT
    env: list[str] = field(default_factory=list)
    env_file: list[str] = field(default_factory=list)
    workspace: Path | None = None
    out_dir: Path | None = None
    keep_sandboxes: bool = False
    reset: bool = True
    strict: bool = False
    dry_run: bool = False


@dataclass
class BenchmarkPlan:
    """What a benchmark run *would* do — printed as the preamble / `--dry-run`."""

    configs_dir: Path
    candidates: list[str]
    max_level: int
    baseline: bool
    baseline_model: str | None
    judge: bool
    judge_model: str | None
    agent: str
    workspace: Path
    out_dir: Path
    sweep_sandbox: str
    baseline_sandbox: str | None


@dataclass
class BenchmarkResult:
    plan: BenchmarkPlan
    dry_run: bool
    results: list[SweepResult] = field(default_factory=list)
    index: Path | None = None
    results_json: Path | None = None
    strict_failed: bool = False


def discover_candidates(configs_dir: Path) -> list[Path]:
    """The candidate config dirs under `configs_dir`: each immediate subdir that holds
    a `.opencode/` tree. Sorted by name (stable, like the model sweep). Fails loud on a
    missing dir or an empty set (Working Rule 8) rather than running zero configs."""
    base = Path(configs_dir)
    if not base.is_dir():
        raise FileNotFoundError(f"benchmark configs dir not found: {base}")
    cands = sorted(d for d in base.iterdir() if d.is_dir() and (d / _OPENCODE_DIR).is_dir())
    if not cands:
        raise ValueError(
            f"no candidate configs in {base}: each candidate is a subdirectory containing a "
            f"'{_OPENCODE_DIR}/' tree (opencode.jsonc + any agent .md)."
        )
    return cands


def apply_config(workspace_root: Path, candidate_dir: Path) -> None:
    """Replace the workspace's `.opencode/` with the candidate's, so the next battery
    run uses that candidate's config. Host-side file op on the validator-owned
    workspace (like `seed_workspace`); the copied tree is untracked and is cleaned by
    the next guarded `reset_workspace`."""
    dest = workspace_root / _OPENCODE_DIR
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(candidate_dir / _OPENCODE_DIR, dest)


def _benchmark_names(target: Path) -> tuple[str, str]:
    base = sb.default_name(target.resolve(), sb.DEFAULT_AGENT)
    return f"{base}-benchmark", f"{base}-benchmark-claude"


def _variant(name: str) -> ConfigVariant:
    """A benchmark variant: identified by the candidate's dir name, with NO model ref
    (empty ⇒ `run_tiers` passes no `-m`, so the candidate's own opencode.jsonc decides
    the model). `description` carries the label the report shows in place of a ref."""
    return ConfigVariant(model_name=name, model_ref="", description=f"config:{name}")


def _resolve_plan(
    opts: BenchmarkOptions, candidates: list[Path], *, timestamp: str
) -> BenchmarkPlan:
    target = opts.target.resolve()
    sweep_name, baseline_name = _benchmark_names(target)
    workspace = (opts.workspace or Path(tempfile.gettempdir()) / sweep_name).resolve()
    out_dir = (opts.out_dir or Path(".danno-benchmark") / timestamp).resolve()
    return BenchmarkPlan(
        configs_dir=opts.configs_dir.resolve(),
        candidates=[c.name for c in candidates],
        max_level=opts.max_level,
        baseline=opts.baseline,
        baseline_model=opts.baseline_model,
        judge=opts.judge,
        judge_model=opts.judge_model,
        agent=opts.agent,
        workspace=workspace,
        out_dir=out_dir,
        sweep_sandbox=sweep_name,
        baseline_sandbox=baseline_name if opts.baseline else None,
    )


def _run_meta(plan: BenchmarkPlan, opts: BenchmarkOptions) -> dict[str, object]:
    return {
        "command": "benchmark",
        "configs_dir": str(plan.configs_dir),
        "candidates": plan.candidates,
        "max_level": plan.max_level,
        "baseline": plan.baseline,
        "baseline_model": plan.baseline_model,
        "judge": plan.judge,
        "judge_model": plan.judge_model,
        "agent": plan.agent,
        "reset": opts.reset,
    }


def run_benchmark(
    config: DannoConfig,
    opts: BenchmarkOptions,
    runner: Runner,
    *,
    now: datetime | None = None,
    version: str | None = None,
) -> BenchmarkResult:
    """Run the benchmark end to end and return its `BenchmarkResult`.

    `config` supplies sandbox/workspace orchestration (`[sandbox]`, env) exactly as
    `validate` uses danno.toml; the per-candidate opencode config comes from the
    candidate dirs, not `config`. `now`/`version` are injected for deterministic tests.

    `benchmark` is opencode-only by construction: a candidate is a `.opencode/` tree
    (`apply_config`) and every tier is driven by `_authed_opencode_run`. claurst has no
    candidate-config analog, so `--agent claurst` (or any non-opencode AUT) is rejected
    loud here rather than provisioning that agent and then silently driving it as
    opencode. claurst remains a first-class AUT in `validate` and `bench`, which sweep
    danno.toml's models (not config trees).
    """
    if opts.agent != sb.DEFAULT_AGENT:
        raise ValueError(
            f"`danno benchmark` compares opencode config trees and only supports "
            f"--agent {sb.DEFAULT_AGENT}, not {opts.agent!r}. To benchmark {opts.agent} "
            f"across your danno.toml models, use `danno bench --agent {opts.agent}`."
        )
    now = now or datetime.now(UTC)
    version = version or _danno_version()
    timestamp = now.strftime("%Y-%m-%dT%H-%M-%S")

    candidates = discover_candidates(opts.configs_dir)
    plan = _resolve_plan(opts, candidates, timestamp=timestamp)
    log_info(
        f"benchmark plan: {len(plan.candidates)} configs {plan.candidates} "
        f"→ L0–L{plan.max_level}{' + baseline' if plan.baseline else ''} → {plan.out_dir}"
    )
    if opts.baseline:
        sb.agent_env("claude", sb.DEFAULT_OLLAMA_URL)  # fail loud on missing claude auth
    judge = _build_judge(opts.judge, opts.judge_model)  # fail loud on missing key/SDK
    if opts.dry_run:
        return BenchmarkResult(plan=plan, dry_run=True)

    level1, level2 = _levels(opts.max_level)

    log_info(f"prepare workspace  {plan.workspace}")
    prepare_workspace(runner, plan.workspace, config)
    log_info(f"provision {opts.agent} sandbox  {plan.sweep_sandbox}")
    sb.provision(runner, plan.sweep_sandbox, plan.workspace, agent=opts.agent, registry_path=None)

    env_file = (
        sb._build_env_file([], opts.env, opts.env_file) if (opts.env or opts.env_file) else None
    )
    run_turn = _authed_opencode_run(env_file) if env_file is not None else None
    results: list[SweepResult] = []
    try:
        for candidate in candidates:
            if opts.reset:
                reset_workspace(runner, plan.sweep_sandbox, plan.workspace)
            apply_config(plan.workspace, candidate)
            log_info(f"benchmark config: {candidate.name}")
            results.append(
                run_tiers(
                    runner,
                    plan.sweep_sandbox,
                    variant=_variant(candidate.name),
                    workspace_root=plan.workspace,
                    level1=level1,
                    level2=level2,
                    run_turn=run_turn,
                    judge=judge,
                )
            )
    finally:
        if env_file is not None:
            env_file.unlink(missing_ok=True)

    if opts.baseline and plan.baseline_sandbox is not None:
        log_info(f"provision claude sandbox  {plan.baseline_sandbox}")
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
                judge=judge,
            )
        )

    if not opts.keep_sandboxes:
        _teardown(runner, plan.sweep_sandbox)
        if plan.baseline_sandbox is not None:
            _teardown(runner, plan.baseline_sandbox)

    _, index = write_sweep_report(results, plan.out_dir)
    record = run_record(
        results,
        config_path=plan.configs_dir,
        declared_models=plan.candidates,
        run_meta=_run_meta(plan, opts),
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        danno_version=version,
        requested_baseline_model=opts.baseline_model,
    )
    results_json = write_results_json(record, plan.out_dir / "results.json")

    strict_failed = opts.strict and not all(_config_ok(s, opts.max_level) for s in results)
    if strict_failed:
        log_warn("benchmark --strict: at least one config failed its requested tiers")
    return BenchmarkResult(
        plan=plan,
        dry_run=False,
        results=results,
        index=index,
        results_json=results_json,
        strict_failed=strict_failed,
    )
