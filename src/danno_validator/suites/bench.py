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

from book_em_danno.capture.wiring import (
    CaptureBinding,
    capture_allow_hosts,
    perm_segment,
    plan_capture,
    uncaptured_cloud_refs,
)
from book_em_danno.commands import sandbox as sb
from book_em_danno.config.generate import generate
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
from danno_validator.telemetry.provenance import collect_provenance, write_provenance
from danno_validator.telemetry.report import write_report
from danno_validator.telemetry.sampler import SampleBinding, summary_to_dict
from danno_validator.telemetry.wire_metrics import TurnWireMetrics, headroom_pct


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
    capture: bool = False
    capture_dir: Path | None = None
    sample: bool = False
    sample_interval: float = 0.5


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


def _setup_bench_capture(
    config: DannoConfig, opts: BenchOptions, out_dir: Path
) -> tuple[DannoConfig, CaptureBinding | None, tuple[str, ...], int | None]:
    """Resolve `--capture` for bench: (config to run from, per-permutation binding,
    sandbox allow-list, occ/claurst relay upstream port).

    Off → the original config, no binding, the default egress allow-list, no relay
    port. On → rewrite each redirectable backend's base_url at a recording proxy
    (`plan_capture`, stable ports baked into provisioning) and open its port; the
    returned `CaptureBinding` mints a per-permutation JSONL per turn. Warns loud
    (Working Rule 8) about traffic capture cannot reach: built-in cloud refs and the
    cloud claude reference row (api.anthropic.com has no danno base_url lever)."""
    if not opts.capture:
        return config, None, sb.DEFAULT_ALLOW_HOSTS, None
    capture_dir = opts.capture_dir or (out_dir / "captures")
    cfg_for_run, targets = plan_capture(config, capture_dir)
    log_info(f"--capture: recording bench <-> backend wire traffic under {capture_dir}")
    uncap = uncaptured_cloud_refs(config)
    if uncap:
        log_warn(
            "--capture cannot record built-in cloud refs (no danno base_url lever): "
            f"{', '.join(uncap)}"
        )
    if opts.agent == CLAUDE:
        log_warn("--capture does not record the claude reference row (api.anthropic.com).")
    binding = CaptureBinding(targets=tuple(targets), capture_dir=capture_dir)
    allow = capture_allow_hosts(targets, sb.DEFAULT_ALLOW_HOSTS)
    return cfg_for_run, binding, allow, binding.ollama_port(config)


def _setup_bench_sampler(opts: BenchOptions, out_dir: Path) -> SampleBinding | None:
    """Resolve `--sample`: a per-permutation resource sampler rooted at
    `<out_dir>/samples`, or None when off. Host-side polling of `localhost:11434`
    (`/api/ps`) and `nvidia-smi`; degrades gracefully off the Linux/NVIDIA host."""
    if not opts.sample:
        return None
    sample_dir = out_dir / "samples"
    log_info(
        f"--sample: profiling host CPU/GPU/mem/VRAM every {opts.sample_interval}s "
        f"under {sample_dir}"
    )
    return SampleBinding(sample_dir=sample_dir, interval=opts.sample_interval)


def _seed_opencode_config(config: DannoConfig, agent: str, workspace: Path) -> None:
    """Generate `.opencode/opencode.jsonc` into the bench workspace for opencode.

    Only opencode reads this file — it declares the `ollama`/openai providers (with
    `host.docker.internal:11434`, which the sandbox's egress proxy rewrites to
    `localhost`) and the model registry, so a `-m ollama/<tag>` turn resolves. The
    `validate` sweep seeds it via `prepare_workspace`, but bench never did — so
    every opencode turn failed with "Model not found: ollama/<tag>". claurst/occ/
    claude don't read opencode.jsonc (they dial Ollama through the in-VM relay or a
    cloud provider), so this is a no-op for them. `disable_title` matches the sweep:
    no throwaway per-session title-gen call against the local model."""
    if agent != sb.DEFAULT_AGENT:  # "opencode"
        return
    generate(config, workspace, apply=True, disable_title=True)


def _run_aider(
    runner: Runner,
    cfg: BenchmarksConfig,
    opts: BenchOptions,
    *,
    workspace: Path,
    variants: list[ConfigVariant],
    config: DannoConfig,
    env_file: Path | None,
    capture: CaptureBinding | None,
    sampler: SampleBinding | None,
    allow_hosts: tuple[str, ...],
    capture_port: int | None,
) -> list[BenchVerdict]:
    ap = cfg.aider_polyglot
    if not (ap.enabled and ap.select):
        return []
    name = _sandbox_name(opts.target, "aider")
    image = resolve_image(opts.agent)
    log_info(f"[bench] aider — provision {opts.agent} sandbox '{name}'")
    sb.provision(runner, name, workspace, agent=image, allow_hosts=allow_hosts)
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
                run_turn=run_turn_for(opts.agent, env_file, capture_port),
                model=variant.model_ref,
                capture=capture,
                sampler=sampler,
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
    capture: CaptureBinding | None,
    sampler: SampleBinding | None,
    allow_hosts: tuple[str, ...],
    capture_port: int | None,
) -> list[BenchVerdict]:
    sw = cfg.swebench
    if not (sw.enabled and sw.select):
        return []
    tasks = load_swebench_tasks(sw.select, dataset=sw.dataset, deps=sw.deps)
    verdicts: list[BenchVerdict] = []
    for task in tasks:
        name = _sandbox_name(opts.target, f"swe-{task.id}")
        log_info(f"[bench] swebench {task.id} — provision {opts.agent} sandbox '{name}'")
        sb.provision(
            runner, name, workspace, agent=resolve_image(opts.agent), allow_hosts=allow_hosts
        )
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
                            run_turn_for(opts.agent, env_file, capture_port),
                            task.workspace_dir(workspace),
                        ),
                        capture=capture,
                        sampler=sampler,
                    )
                )
        finally:
            _teardown(runner, name, keep=opts.keep_sandboxes)
    return verdicts


def _num_ctx_by_model(provenance: dict) -> dict[str, int | None]:
    """`{model_ref: context_length}` from provenance §7.2 — the model's real loaded
    ceiling, used to compute §6.3 headroom in each row (NOT opencode's `context_budget`)."""
    models = provenance.get("models") or {}
    return {ref: (facts or {}).get("context_length") for ref, facts in models.items()}


def _wire_summary(wire: TurnWireMetrics | None, num_ctx: int | None) -> dict | None:
    """The rollup slice of a turn's wire metrics for `bench.json` (the full per-request
    series stays in the `metrics/` sidecar). Headroom is filled here against `num_ctx`."""
    if wire is None:
        return None
    return {
        "request_count": wire.request_count,
        "input_tokens": wire.input_tokens,
        "output_tokens": wire.output_tokens,
        "cached_tokens": wire.cached_tokens,
        "total_tokens": wire.total_tokens,
        "tok_per_s": wire.tok_per_s,
        "ttft_s": wire.ttft_s,
        "ttft_label": wire.ttft_label,
        "rtt_min_s": wire.rtt_min_s,
        "rtt_max_s": wire.rtt_max_s,
        "rtt_mean_s": wire.rtt_mean_s,
        "peak_ctx_tokens": wire.peak_ctx_tokens,
        "ctx_headroom_pct": headroom_pct(wire.peak_ctx_tokens, num_ctx),
        "ctx_growth": wire.ctx_growth,
        "ctx_deltas": wire.ctx_deltas,
    }


def _sidecars(v: BenchVerdict, *, out_dir: Path, capture_dir: Path | None) -> dict:
    """Relative paths (from `out_dir`) to this permutation's sidecar artifacts, so
    `bench.json` stays the index into the raw `captures/metrics/transcripts/samples`.
    Only families that were actually written for the row are included."""
    seg = perm_segment(v.suite, v.task_id, v.model)  # <suite>/<task>/<slug>
    out: dict = {}
    if v.wire is not None:
        out["metrics"] = f"metrics/{seg.with_suffix('.json')}"
        out["transcript"] = f"transcripts/{seg.with_suffix('.md')}"
        cap_dir = capture_dir or (out_dir / "captures")
        cap_parent = cap_dir / seg.parent
        caps = sorted(cap_parent.glob(f"{seg.name}.*.jsonl")) if cap_parent.is_dir() else []
        rels: list[str] = []
        for cap in caps:
            try:
                rels.append(str(cap.relative_to(out_dir)))
            except ValueError:
                rels.append(str(cap))
        if rels:
            out["captures"] = rels
    if v.resource is not None:
        out["samples"] = f"samples/{seg.with_suffix('.jsonl')}"
    return out


def _result_row(
    v: BenchVerdict,
    *,
    num_ctx_by_model: dict[str, int | None],
    out_dir: Path,
    capture_dir: Path | None,
) -> dict:
    """One `bench.json` row: today's flat fields (unchanged, additive) plus the `model`
    axis and the `wire`/`resource`/`sidecars` sub-objects when captured/sampled."""
    row: dict = {
        "suite": v.suite,
        "task": v.task_id,
        "model": v.model,
        "passed": v.passed,
        "verdict": str(v.verdict.failure_class),
        "tool_calls": v.tool_calls,
        "tokens": v.tokens,
        "cost": v.cost,
        "latency_s": round(v.latency_s, 1),
        "error": v.error_summary,
    }
    wire = _wire_summary(v.wire, num_ctx_by_model.get(v.model or ""))
    if wire is not None:
        row["wire"] = wire
    if v.resource is not None:
        row["resource"] = summary_to_dict(v.resource)
    sidecars = _sidecars(v, out_dir=out_dir, capture_dir=capture_dir)
    if sidecars:
        row["sidecars"] = sidecars
    return row


def _build_results_payload(
    report: BenchReport,
    *,
    config_path: Path,
    agent: str,
    variants: list[ConfigVariant],
    num_ctx_by_model: dict[str, int | None],
    capture_dir: Path | None,
) -> dict:
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": str(config_path),
        "agent": agent,
        "models": [v.model_ref for v in variants],
        "results": [
            _result_row(
                v,
                num_ctx_by_model=num_ctx_by_model,
                out_dir=report.out_dir,
                capture_dir=capture_dir,
            )
            for v in report.verdicts
        ],
    }


def _write_results(
    report: BenchReport,
    *,
    config_path: Path,
    agent: str,
    variants: list[ConfigVariant],
    num_ctx_by_model: dict[str, int | None] | None = None,
    capture_dir: Path | None = None,
) -> Path:
    report.out_dir.mkdir(parents=True, exist_ok=True)
    path = report.out_dir / "bench.json"
    payload = _build_results_payload(
        report,
        config_path=config_path,
        agent=agent,
        variants=variants,
        num_ctx_by_model=num_ctx_by_model or {},
        capture_dir=capture_dir,
    )
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

    # `--capture`: rewrite redirectable backends' base_urls at recording proxies (stable
    # ports) and open their egress ports; the binding mints a per-permutation JSONL per
    # turn. Off → original config, no binding, default egress. opencode's provider file
    # must be generated from the REWRITTEN config so its base_url dials the proxy.
    cfg_for_run, capture, allow_hosts, capture_port = _setup_bench_capture(config, opts, out_dir)
    sampler = _setup_bench_sampler(opts, out_dir)
    _seed_opencode_config(cfg_for_run, opts.agent, workspace)

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
            config=cfg_for_run,
            env_file=env_file,
            capture=capture,
            sampler=sampler,
            allow_hosts=allow_hosts,
            capture_port=capture_port,
        )
        report.verdicts += _run_swebench(
            runner,
            bench_cfg,
            opts,
            workspace=workspace,
            variants=variants,
            config=cfg_for_run,
            env_file=env_file,
            capture=capture,
            sampler=sampler,
            allow_hosts=allow_hosts,
            capture_port=capture_port,
        )
    finally:
        if env_file is not None:
            env_file.unlink(missing_ok=True)

    # §7 provenance is always written (a separate file, so bench.json's schema is stable):
    # exact model bytes + static facts, agent/danno pins, host descriptor, sampler interval.
    # Collected BEFORE the results so each row's §6.3 headroom can compare peak context
    # against the model's real loaded `context_length`.
    provenance = collect_provenance(
        config,
        variants,
        agent=opts.agent,
        sample_interval_s=opts.sample_interval if opts.sample else None,
    )
    write_provenance(out_dir, provenance)
    report.results_json = _write_results(
        report,
        config_path=opts.target / "danno.toml",
        agent=opts.agent,
        variants=variants,
        num_ctx_by_model=_num_ctx_by_model(provenance),
        capture_dir=opts.capture_dir,
    )
    # Human report (summary + per-permutation detail). `report.html` is self-contained
    # so it doubles as the published Artifact summary.
    payload = json.loads(report.results_json.read_text(encoding="utf-8"))
    md_path, html_path = write_report(out_dir, payload, provenance=provenance)
    _summary(report.verdicts)
    log_info(f"\n  results  {report.results_json}")
    log_info(f"  report   {md_path}  ·  {html_path}")
    return report
