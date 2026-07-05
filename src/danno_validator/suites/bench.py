"""`danno bench` orchestration: run the enabled benchmark suites across the model
matrix (the "permutations") against one agent-under-test.

Standalone from `danno validate` (the user's call): it provisions disposable,
validator-owned sandboxes over a throwaway workspace, runs each enabled suite for
every model variant of the project's danno.toml, writes `bench.json` + a summary,
and tears the sandboxes down. Aider Polyglot shares one sandbox (per-exercise reset);
SWE-bench uses a fresh sandbox per instance (its own repo + dep tree).
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from book_em_danno.commands import sandbox as sb
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import CommandFailedError, Runner, log_info, log_warn
from danno_validator import baseline
from danno_validator.driver import seed_workspace
from danno_validator.matrix import ConfigVariant, model_variants
from danno_validator.suites.aut import CLAUDE, install_aut, resolve_image, run_turn_for
from danno_validator.suites.base import BenchVerdict, error_verdict, run_bench_task
from danno_validator.suites.config import BenchmarksConfig
from danno_validator.suites.run import (
    clone_polyglot,
    cwd_bound,
    remove_checkout,
    run_aider_suite,
    temp_checkout_dir,
)
from danno_validator.suites.swebench import load_swebench_tasks


@dataclass
class BenchOptions:
    target: Path
    agent: str = "opencode"
    only: list[str] | None = None
    benchmarks_path: Path | None = None
    workspace: Path | None = None
    out_dir: Path | None = None
    keep_sandboxes: bool = False
    dry_run: bool = False


@dataclass
class BenchReport:
    out_dir: Path
    verdicts: list[BenchVerdict] = field(default_factory=list)
    dry_run: bool = False
    results_json: Path | None = None


def _sandbox_name(target: Path, suffix: str) -> str:
    """A disposable bench sandbox name — short by necessity.

    Docker Desktop's sandbox VM socket path (`~/.docker/sandboxes/vm/<name>/eth`)
    has a hard ~94-char limit, and SWE-bench instance ids are long, so the name is
    kept compact: a readable truncated suffix plus an 8-char hash of the full
    (project, suffix) for uniqueness. Capped well under the limit for any username.
    """
    base = sb.default_name(target.resolve(), sb.DEFAULT_AGENT)
    digest = hashlib.sha1(f"{base}-{suffix}".encode()).hexdigest()[:8]
    short = re.sub(r"[^A-Za-z0-9]+", "-", suffix).strip("-")[:16].strip("-")
    return f"danno-bench-{short}-{digest}"


def _teardown(runner: Runner, name: str, *, keep: bool) -> None:
    if keep:
        return
    runner.advise(["docker", "sandbox", "stop", name], why=f"stop bench sandbox '{name}'")
    runner.advise(["docker", "sandbox", "rm", name], why=f"remove bench sandbox '{name}'")


def _build_bench_env_file(config: DannoConfig, agent: str) -> Path | None:
    """The chmod-600 env-file bound into every bench exec for `agent`, or `None`.

    This is what carries occ's tunable loop ceilings (`CLAUDE_CODE_API_TIMEOUT`,
    `CLAUDE_CODE_MAX_RECURSION_DEPTH`) and any cloud-provider keys declared via
    `danno.toml [env]` (`{env:VAR}` indirection) into bench turns — previously bench
    passed `None`, so `[env]` never reached the AUT (only interactive + validate did).

    claude is special: it does NOT flow through `assemble_agent_env` (its auth stays
    in `agent_env`), so its file is built exactly like the baseline's — failing loud
    (Working Rule 8) when no host `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` is set.
    For the other agents the AUT's own level-4 defaults (e.g. occ's knobs, opencode's
    `OLLAMA_BASE_URL`) seed the file and `danno.toml [env]` composes on top; `None`
    only when there is genuinely nothing to inject."""
    if agent == CLAUDE:
        return baseline._build_claude_auth_env_file()  # fails loud without a host token
    defaults = sb.agent_env(agent, sb.DEFAULT_OLLAMA_URL)
    lines = sb.assemble_agent_env(config, agent_defaults=defaults, env_pairs=[], env_files=[])
    if not lines:
        return None
    return sb._build_env_file(lines, [], [])


def _run_aider(
    runner: Runner,
    cfg: BenchmarksConfig,
    opts: BenchOptions,
    *,
    workspace: Path,
    variants: list[ConfigVariant],
    config: DannoConfig,
    env_file: Path | None,
) -> list[BenchVerdict]:
    ap = cfg.aider_polyglot
    if not (ap.enabled and ap.select):
        return []
    name = _sandbox_name(opts.target, "aider")
    image = resolve_image(opts.agent)
    log_info(f"[bench] aider — provision {opts.agent} sandbox '{name}'")
    sb.provision(runner, name, workspace, agent=image)
    install_aut(runner, name, opts.agent, config)
    checkout = clone_polyglot(runner, ap.source, temp_checkout_dir())
    verdicts: list[BenchVerdict] = []
    try:
        for variant in variants:
            log_info(f"[bench] aider × {variant.model_ref}")
            verdicts += run_aider_suite(
                runner,
                name,
                checkout=checkout,
                select=ap.select,
                workspace=workspace,
                run_turn=run_turn_for(opts.agent, env_file),
                model=variant.model_ref,
            )
    finally:
        remove_checkout(checkout)
        _teardown(runner, name, keep=opts.keep_sandboxes)
    return verdicts


def _run_swebench(
    runner: Runner,
    cfg: BenchmarksConfig,
    opts: BenchOptions,
    *,
    workspace: Path,
    variants: list[ConfigVariant],
    config: DannoConfig,
    env_file: Path | None,
) -> list[BenchVerdict]:
    sw = cfg.swebench
    if not (sw.enabled and sw.select):
        return []
    tasks = load_swebench_tasks(sw.select, dataset=sw.dataset, deps=sw.deps)
    verdicts: list[BenchVerdict] = []
    for task in tasks:
        name = _sandbox_name(opts.target, f"swe-{task.id}")
        log_info(f"[bench] swebench {task.id} — provision {opts.agent} sandbox '{name}'")
        sb.provision(runner, name, workspace, agent=resolve_image(opts.agent))
        install_aut(runner, name, opts.agent, config)
        try:
            try:
                task.provision(runner, name, workspace)
            except CommandFailedError as exc:
                # One instance's repo/deps failing must not abort the whole run —
                # record an errored row per model variant and move on (fail loud,
                # but per-row, as the suite promises).
                log_warn(f"[bench] swebench {task.id} provision failed: {exc}")
                verdicts += [
                    error_verdict(task.id, "swebench", f"provision failed: {exc}") for _ in variants
                ]
                continue
            for variant in variants:
                log_info(f"[bench] swebench {task.id} × {variant.model_ref}")
                verdicts.append(
                    run_bench_task(
                        runner,
                        name,
                        task=task,
                        suite="swebench",
                        workspace=workspace,
                        model=variant.model_ref,
                        run_turn=cwd_bound(
                            run_turn_for(opts.agent, env_file), task.workspace_dir(workspace)
                        ),
                    )
                )
        finally:
            _teardown(runner, name, keep=opts.keep_sandboxes)
    return verdicts


def _write_results(
    report: BenchReport, *, config_path: Path, agent: str, variants: list[ConfigVariant]
) -> Path:
    report.out_dir.mkdir(parents=True, exist_ok=True)
    path = report.out_dir / "bench.json"
    payload = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": str(config_path),
        "agent": agent,
        "models": [v.model_ref for v in variants],
        "results": [
            {
                "suite": v.suite,
                "task": v.task_id,
                "passed": v.passed,
                "verdict": str(v.verdict.failure_class),
                "tool_calls": v.tool_calls,
                "tokens": v.tokens,
                "cost": v.cost,
                "latency_s": round(v.latency_s, 1),
                "error": v.error_summary,
            }
            for v in report.verdicts
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _summary(verdicts: list[BenchVerdict]) -> None:
    if not verdicts:
        log_warn("[bench] no suites enabled/selected — nothing ran")
        return
    passed = sum(1 for v in verdicts if v.passed)
    log_info(f"\n── bench results ──  {passed}/{len(verdicts)} passed")
    for v in verdicts:
        mark = "✓ pass" if v.passed else f"✗ {v.verdict.failure_class}"
        log_info(f"  {v.suite:9} {v.task_id:40} {mark}  ({v.latency_s:.0f}s)")


def run_bench(
    config: DannoConfig,
    bench_cfg: BenchmarksConfig,
    opts: BenchOptions,
    runner: Runner,
    *,
    now: datetime | None = None,
) -> BenchReport:
    """Run the enabled suites across the model matrix against `opts.agent`."""
    now = now or datetime.now(UTC)
    # claude is the cloud reference AUT: it ignores `-m`, so its matrix collapses to a
    # single `claude-code` row rather than one per local model (see baseline_variant).
    if opts.agent == CLAUDE:
        variants = [baseline.baseline_variant(None)]
    else:
        variants = model_variants(config, only=opts.only)
    out_dir = opts.out_dir or Path(".danno-bench") / now.strftime("%Y-%m-%dT%H-%M-%S")
    report = BenchReport(out_dir=out_dir, dry_run=opts.dry_run)

    log_info(
        f"danno bench — agent={opts.agent} · models={[v.model_ref for v in variants]} · "
        f"aider={'on' if bench_cfg.aider_polyglot.enabled else 'off'} "
        f"swebench={'on' if bench_cfg.swebench.enabled else 'off'}"
    )
    if opts.dry_run:
        log_info("(--dry-run; drop it to provision and run)")
        return report

    workspace = opts.workspace or Path(tempfile.gettempdir()) / _sandbox_name(opts.target, "ws")
    workspace = workspace.resolve()
    seed_workspace(workspace)

    # One agent-scoped, model-independent env-file for the whole run: it carries occ's
    # loop-ceiling knobs + cloud keys ([env]) — or claude's auth — into every turn. Built
    # up front so a missing claude token fails loud before any sandbox is provisioned.
    env_file = _build_bench_env_file(config, opts.agent)
    try:
        report.verdicts += _run_aider(
            runner,
            bench_cfg,
            opts,
            workspace=workspace,
            variants=variants,
            config=config,
            env_file=env_file,
        )
        report.verdicts += _run_swebench(
            runner,
            bench_cfg,
            opts,
            workspace=workspace,
            variants=variants,
            config=config,
            env_file=env_file,
        )
    finally:
        if env_file is not None:
            env_file.unlink(missing_ok=True)

    report.results_json = _write_results(
        report, config_path=opts.target / "danno.toml", agent=opts.agent, variants=variants
    )
    _summary(report.verdicts)
    log_info(f"\n  results  {report.results_json}")
    return report
