"""`danno bench` orchestration: run the enabled benchmark suites across the model
matrix (the "permutations") against one harness-under-test.

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
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from book_em_danno.capture.wiring import (
    CaptureBinding,
    capture_allow_hosts,
    perm_segment,
    plan_capture,
    uncaptured_cloud_refs,
)
from book_em_danno.commands import ollama
from book_em_danno.commands import sandbox as sb
from book_em_danno.config.generate import generate
from book_em_danno.config.schema import DannoConfig, InertBackend
from book_em_danno.core.exec import CommandFailedError, Runner, log_info, log_warn
from danno_validator import baseline
from danno_validator.driver import seed_workspace
from danno_validator.matrix import ConfigVariant, model_variants
from danno_validator.suites.aut import (
    CLAUDE,
    CLAURST,
    OCC,
    install_harness,
    resolve_image,
    run_turn_for,
)
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
from danno_validator.telemetry.report import (
    load as load_reports,
)
from danno_validator.telemetry.report import (
    merge_html,
    merge_markdown,
    write_report,
)
from danno_validator.telemetry.sampler import SampleBinding, summary_to_dict
from danno_validator.telemetry.wire_metrics import TurnWireMetrics, headroom_pct


@dataclass
class BenchOptions:
    target: Path
    harness: str = "opencode"
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
    warm: bool = True
    env: list[str] = field(default_factory=list)
    env_file: list[str] = field(default_factory=list)


@dataclass
class BenchReport:
    out_dir: Path
    verdicts: list[BenchVerdict] = field(default_factory=list)
    dry_run: bool = False
    results_json: Path | None = None


# The harnesses-under-test `danno bench` can drive (occ/claurst/opencode on the local +
# cloud matrix; claude sweeps its inert-backend models, or one `(default model)` row if
# none are declared). Ordered for a stable report layout.
BENCH_HARNESSES: tuple[str, ...] = (sb.DEFAULT_HARNESS, CLAURST, OCC, CLAUDE)

_OLLAMA_PREFIX = "ollama/"


def resolve_bench_harnesses(
    cli_harnesses: list[str] | None, bench_cfg: BenchmarksConfig
) -> list[str]:
    """The de-duplicated, order-preserving list of harnesses to benchmark, with precedence
    `--harness` (CLI) > `benchmarks.toml [harnesses]` > the single opencode default.

    Fails loud (Working Rule 8) on an unknown harness name from either source, naming the
    valid set, rather than provisioning a sandbox for a typo."""
    chosen: list[str] = list(cli_harnesses or bench_cfg.harnesses) or [sb.DEFAULT_HARNESS]
    seen: dict[str, None] = {}
    for name in chosen:
        if name not in BENCH_HARNESSES:
            raise ValueError(
                f"unknown --harness '{name}'. Valid harnesses: {', '.join(BENCH_HARNESSES)}."
            )
        seen.setdefault(name, None)
    return list(seen)


def _sandbox_name(target: Path, suffix: str) -> str:
    """A disposable bench sandbox name — short by necessity.

    Docker Desktop's sandbox VM socket path (`~/.docker/sandboxes/vm/<name>/eth`)
    has a hard ~94-char limit, and SWE-bench instance ids are long, so the name is
    kept compact: a readable truncated suffix plus an 8-char hash of the full
    (project, suffix) for uniqueness. Capped well under the limit for any username.
    """
    base = sb.default_name(target.resolve(), sb.DEFAULT_HARNESS)
    digest = hashlib.sha1(f"{base}-{suffix}".encode()).hexdigest()[:8]
    short = re.sub(r"[^A-Za-z0-9]+", "-", suffix).strip("-")[:16].strip("-")
    return f"danno-bench-{short}-{digest}"


def _teardown(runner: Runner, name: str, *, keep: bool) -> None:
    if keep:
        return
    runner.advise(["docker", "sandbox", "stop", name], why=f"stop bench sandbox '{name}'")
    runner.advise(["docker", "sandbox", "rm", name], why=f"remove bench sandbox '{name}'")


def _variant_cloud_env_lines(harness: str, config: DannoConfig, model_name: str) -> list[str]:
    """The cloud-provider auth env-file lines for one bench variant, or [] for a local model.

    Reuses the exact builders `danno sandbox start` uses, so bench authenticates a cloud
    model identically per harness: occ needs the `OPENAI_BASE_URL`/`OPENAI_API_KEY` mapping;
    claurst and opencode read the provider key under its own `{api_key_env}` name (opencode's
    generated provider block references `{env:<api_key_env>}`). Fails loud (Working Rule 8)
    when the key var is unset. `model_name` is the danno.toml `[models]` key (a raw
    `<provider>/<tag>` ref, which the matrix never yields for a cloud row, resolves to [])."""
    if harness == OCC:
        return sb.occ_cloud_env_lines(config, model_name)
    if harness == CLAURST:
        return sb.claurst_cloud_env_lines(config, model_name)
    if harness == sb.DEFAULT_HARNESS:  # opencode
        return sb.cloud_api_key_env_lines(config, model_name)
    return []  # claude: the cloud reference HUT carries its own auth (never a [models] key)


def _claude_inert_models(config: DannoConfig, only: Sequence[str] | None) -> list[str]:
    """The declared inert-backend model names claude should sweep, sorted (`only`-filtered).

    claude selects its model by `--model`, so only inert-backend [models] (whose `tag` is
    the raw `--model` value) are meaningful to it — local/cloud OpenAI-compatible models
    are opencode/occ/claurst's matrix, not claude's. Empty → the caller falls back to the
    single install-default reference row."""
    names = [
        n
        for n in sorted(config.models)
        if isinstance(config.backends[config.models[n].backend], InertBackend)
    ]
    if only is not None:
        keep = set(only)
        names = [n for n in names if n in keep]
    return names


def _harness_dial_ref(harness: str, config: DannoConfig, variant: ConfigVariant) -> str | None:
    """The ref occ/claurst must actually dial for `variant`, or None (report ref stands).

    The matrix reports the generic `<backend>/<tag>` ref (`variant.model_ref`) so the
    comparison grid and §6.3 headroom lookups key consistently. But occ/claurst detect
    local-vs-cloud by the ref's leading segment (`startswith("ollama/")`), so an Ollama
    backend named anything other than the literal `ollama` (e.g. `danno-ollama/…`) is
    misread as cloud and falls back to Anthropic — the item-3 bug. Reuse the exact
    resolvers `danno sandbox start` dials with (`resolve_occ_model`/`resolve_claurst_model`,
    from the `[models]` name) so bench dials a ref their locality check understands: Ollama
    → `ollama/<tag>`, cloud → each harness's own provider namespace. opencode (whose provider
    is the backend name in the generated `opencode.jsonc`) and claude need no override."""
    if harness == OCC:
        return sb.resolve_occ_model(config, variant.model_name)
    if harness == CLAURST:
        return sb.resolve_claurst_model(config, variant.model_name)
    if harness == CLAUDE:
        # claude picks its model by `--model <alias|id>`, not an OpenAI-compatible ref.
        # Only an INERT-backend model's tag is that value (e.g. "claude-opus-4-8"); a
        # local/cloud model or the synthetic reference row (`baseline_variant`, whose
        # model_name isn't in [models]) → None → claude's install default.
        model = config.models.get(variant.model_name)
        if model is not None and isinstance(config.backends[model.backend], InertBackend):
            return model.tag
        return None
    return None


def _merge_env_lines(base: list[str], extra: list[str]) -> list[str]:
    """`KEY=VAL` lines with `extra` (per-variant cloud auth) overriding `base` on collision."""
    merged: dict[str, str] = {}
    for line in (*base, *extra):
        if "=" in line:
            key, val = line.split("=", 1)
            merged[key] = val
    return [f"{key}={val}" for key, val in merged.items()]


def _build_bench_env_files(
    config: DannoConfig, opts: BenchOptions, variants: list[ConfigVariant]
) -> dict[str, Path | None]:
    """One chmod-600 env-file per model variant (keyed by `model_ref`), bound into that
    variant's bench turns.

    A single base set of lines is shared by every variant — the HUT's level-4 defaults
    (occ's loop ceilings, opencode's `OLLAMA_BASE_URL`) plus `danno.toml [env]` and any
    `--env`/`--env-file`. A CLOUD variant additionally injects its provider auth for the
    HUT (`_variant_cloud_env_lines`), so a mixed local+cloud matrix authenticates each row
    correctly — the previous single harness-scoped file left cloud rows unauthenticated, so
    occ fell back to api.anthropic.com and 404'd. Cloud-key resolution fails loud HERE,
    before any sandbox is provisioned. Files are de-duplicated by content, so local variants
    share one file and all variants on the same cloud backend share another. `config` is the
    (capture-rewritten) config the run drives from, so occ cloud's `OPENAI_BASE_URL` points
    at the `--capture` proxy when capture is on. claude is special: a single model-independent
    auth file (fails loud without a host token, exactly like the baseline)."""
    if opts.harness == CLAUDE:
        auth = baseline._build_claude_auth_env_file()  # fails loud without a host token
        return {v.model_ref: auth for v in variants}
    defaults = sb.harness_env(opts.harness, sb.DEFAULT_OLLAMA_URL)
    base = sb.assemble_harness_env(
        config, harness_defaults=defaults, env_pairs=opts.env, env_files=opts.env_file
    )
    files: dict[str, Path | None] = {}
    by_content: dict[tuple[str, ...], Path | None] = {}
    for v in variants:
        cloud = _variant_cloud_env_lines(opts.harness, config, v.model_name)
        lines = _merge_env_lines(base, cloud) if cloud else base
        content = tuple(lines)
        if content not in by_content:
            by_content[content] = sb._build_env_file(lines, [], []) if lines else None
        files[v.model_ref] = by_content[content]
    return files


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
    if opts.harness == CLAUDE:
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


def _seed_opencode_config(config: DannoConfig, harness: str, workspace: Path) -> None:
    """Generate `.opencode/opencode.jsonc` into the bench workspace for opencode.

    Only opencode reads this file — it declares the `ollama`/openai providers (with
    `host.docker.internal:11434`, which the sandbox's egress proxy rewrites to
    `localhost`) and the model registry, so a `-m ollama/<tag>` turn resolves. The
    `validate` sweep seeds it via `prepare_workspace`, but bench never did — so
    every opencode turn failed with "Model not found: ollama/<tag>". claurst/occ/
    claude don't read opencode.jsonc (they dial Ollama through the in-VM relay or a
    cloud provider), so this is a no-op for them. `disable_title` matches the sweep:
    no throwaway per-session title-gen call against the local model."""
    if harness != sb.DEFAULT_HARNESS:  # "opencode"
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
    env_files: dict[str, Path | None],
    capture: CaptureBinding | None,
    sampler: SampleBinding | None,
    allow_hosts: tuple[str, ...],
    capture_port: int | None,
    warm: bool,
    warmup: list[dict],
) -> list[BenchVerdict]:
    ap = cfg.aider_polyglot
    if not (ap.enabled and ap.select):
        return []
    name = _sandbox_name(opts.target, "aider")
    image = resolve_image(opts.harness)
    log_info(f"[bench] aider — provision {opts.harness} sandbox '{name}'")
    sb.provision(runner, name, workspace, harness=image, allow_hosts=allow_hosts)
    install_harness(runner, name, opts.harness, config)
    checkout = clone_polyglot(runner, ap.source, temp_checkout_dir())
    verdicts: list[BenchVerdict] = []
    try:
        for variant in variants:
            # Warm this model right before its (model-major) block, not all up front — see
            # `_warm_variant`: an up-front bulk warm loses to VRAM eviction between models.
            _warm_variant(variant, warm=warm, warmup=warmup)
            log_info(f"[bench] aider × {variant.model_ref}")
            verdicts += run_aider_suite(
                runner,
                name,
                checkout=checkout,
                select=ap.select,
                workspace=workspace,
                run_turn=run_turn_for(
                    opts.harness,
                    env_files[variant.model_ref],
                    capture_port,
                    model_override=_harness_dial_ref(opts.harness, config, variant),
                ),
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
    env_files: dict[str, Path | None],
    capture: CaptureBinding | None,
    sampler: SampleBinding | None,
    allow_hosts: tuple[str, ...],
    capture_port: int | None,
    warm: bool,
    warmup: list[dict],
) -> list[BenchVerdict]:
    sw = cfg.swebench
    if not (sw.enabled and sw.select):
        return []
    tasks = load_swebench_tasks(sw.select, dataset=sw.dataset, deps=sw.deps)
    verdicts: list[BenchVerdict] = []
    for task in tasks:
        name = _sandbox_name(opts.target, f"swe-{task.id}")
        log_info(f"[bench] swebench {task.id} — provision {opts.harness} sandbox '{name}'")
        sb.provision(
            runner, name, workspace, harness=resolve_image(opts.harness), allow_hosts=allow_hosts
        )
        install_harness(runner, name, opts.harness, config)
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
                # Task-major: the prior task's model may have evicted this one — warm before
                # each cell so an eviction reload is absorbed here (and recorded), not timed.
                _warm_variant(variant, warm=warm, warmup=warmup)
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
                            run_turn_for(
                                opts.harness,
                                env_files[variant.model_ref],
                                capture_port,
                                model_override=_harness_dial_ref(opts.harness, config, variant),
                            ),
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
    harness: str,
    variants: list[ConfigVariant],
    num_ctx_by_model: dict[str, int | None],
    capture_dir: Path | None,
) -> dict:
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "config": str(config_path),
        "harness": harness,
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
    harness: str,
    variants: list[ConfigVariant],
    num_ctx_by_model: dict[str, int | None] | None = None,
    capture_dir: Path | None = None,
) -> Path:
    report.out_dir.mkdir(parents=True, exist_ok=True)
    path = report.out_dir / "bench.json"
    payload = _build_results_payload(
        report,
        config_path=config_path,
        harness=harness,
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


def _warm_variant(variant: ConfigVariant, *, warm: bool, warmup: list[dict]) -> None:
    """Warm this variant's local Ollama model right before its cells run (default on;
    `--no-warm` skips), appending one record (see `ollama.warm_model`) to `warmup`.

    Warming JUST-IN-TIME — per model block, not all models up front — is what makes pre-warm
    survive a multi-model matrix. Two models that don't co-fit in VRAM evict each other, so an
    up-front bulk warm of B would knock A back out of memory before A's model-major block ever
    runs, and A's first cell would pay the reload anyway. Warming immediately before each block
    instead means each model is resident for its own cells. `warm_model` no-ops (`cache_hit`)
    when the tag is still resident (`/api/ps`), so this is a cheap check when the model stayed
    warm and a RECORDED reload when it was evicted — those extra cold-load records are a direct
    eviction/thrash signal (e.g. a task-major swebench matrix alternating two big models).

    Cloud/claude refs have no local model and are skipped. Host-side and best-effort — off the
    sandbox and harness path — so a warm-up failure never aborts the bench."""
    if not warm or not variant.model_ref.startswith(_OLLAMA_PREFIX):
        return
    tag = variant.model_ref[len(_OLLAMA_PREFIX) :]
    result = ollama.warm_model(tag)
    if result["cache_hit"]:
        state = "already resident"
    elif result["warm_load_s"] is not None:
        state = f"loaded in {result['warm_load_s']:g}s"
    else:
        state = "FAILED (cell may pay the load)"
    log_info(f"  warm  {tag}: {state}")
    warmup.append(result)


def run_bench(
    config: DannoConfig,
    bench_cfg: BenchmarksConfig,
    opts: BenchOptions,
    runner: Runner,
    *,
    now: datetime | None = None,
) -> BenchReport:
    """Run the enabled suites across the model matrix against `opts.harness`."""
    now = now or datetime.now(UTC)
    # claude picks its model by `--model`, not the OpenAI-compatible `-m` matrix. So it
    # sweeps only its INERT-backend models (each tag → `--model`); the local ollama/cloud
    # matrix is irrelevant to it. With no inert model declared, it collapses to a single
    # `claude-code` reference row on the install default (see baseline_variant).
    if opts.harness == CLAUDE:
        claude_models = _claude_inert_models(config, opts.only)
        variants = (
            model_variants(config, only=claude_models)
            if claude_models
            else [baseline.baseline_variant(None)]
        )
    else:
        variants = model_variants(config, only=opts.only)
    out_dir = opts.out_dir or Path(".danno-bench") / now.strftime("%Y-%m-%dT%H-%M-%S")
    report = BenchReport(out_dir=out_dir, dry_run=opts.dry_run)

    log_info(
        f"danno bench — harness={opts.harness} · models={[v.model_ref for v in variants]} · "
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
    _seed_opencode_config(cfg_for_run, opts.harness, workspace)

    # Pre-warm accumulator: each suite warms a variant's local model JUST BEFORE its cells run
    # (default on; `--no-warm` skips) so the first cell's latency reflects the harness loop, not
    # a cold load — and so a multi-model matrix survives VRAM eviction (see `_warm_variant`). The
    # per-warm records land in provenance so the report states the run's cold-start posture.
    warmup: list[dict] = []

    # One chmod-600 env-file PER model variant: shared base lines (the HUT's loop-ceiling
    # knobs, opencode's OLLAMA_BASE_URL, danno.toml [env], --env/--env-file) plus each cloud
    # variant's provider auth (occ: OPENAI_BASE_URL/OPENAI_API_KEY; claurst/opencode: the raw
    # {api_key_env}). Built from the capture-rewritten config so occ cloud dials the --capture
    # proxy, and up front so a missing cloud key / claude token fails loud before provisioning.
    env_files = _build_bench_env_files(cfg_for_run, opts, variants)
    try:
        report.verdicts += _run_aider(
            runner,
            bench_cfg,
            opts,
            workspace=workspace,
            variants=variants,
            config=cfg_for_run,
            env_files=env_files,
            capture=capture,
            sampler=sampler,
            allow_hosts=allow_hosts,
            capture_port=capture_port,
            warm=opts.warm,
            warmup=warmup,
        )
        report.verdicts += _run_swebench(
            runner,
            bench_cfg,
            opts,
            workspace=workspace,
            variants=variants,
            config=cfg_for_run,
            env_files=env_files,
            capture=capture,
            sampler=sampler,
            allow_hosts=allow_hosts,
            capture_port=capture_port,
            warm=opts.warm,
            warmup=warmup,
        )
    finally:
        for env_file in {p for p in env_files.values() if p is not None}:
            env_file.unlink(missing_ok=True)

    # §7 provenance is always written (a separate file, so bench.json's schema is stable):
    # exact model bytes + static facts, harness/danno pins, host descriptor, sampler interval.
    # Collected BEFORE the results so each row's §6.3 headroom can compare peak context
    # against the model's real loaded `context_length`.
    provenance = collect_provenance(
        config,
        variants,
        harness=opts.harness,
        sample_interval_s=opts.sample_interval if opts.sample else None,
        warmup=warmup,
    )
    write_provenance(out_dir, provenance)
    report.results_json = _write_results(
        report,
        config_path=opts.target / "danno.toml",
        harness=opts.harness,
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


def run_bench_harnesses(
    config: DannoConfig,
    bench_cfg: BenchmarksConfig,
    opts: BenchOptions,
    runner: Runner,
    harnesses: list[str],
    *,
    now: datetime | None = None,
) -> list[BenchReport]:
    """Run the matrix for each harness in `harnesses`, then (for >1 harness) emit a cross-harness
    comparison report.

    A single harness runs exactly as before — `run_bench` straight into `opts.out_dir`. For
    several harnesses, each runs into its own `<root>/<harness>/` subdir (isolated bench.json +
    sidecars + provenance), sharing one timestamped `root` so the run is one artifact tree;
    a combined `report.md`/`report.html` grid (one column per harness, via the existing
    `report.merge_*`) lands at the root. Per-row fail-loud accounting is unchanged."""
    now = now or datetime.now(UTC)
    if len(harnesses) == 1:
        return [run_bench(config, bench_cfg, replace(opts, harness=harnesses[0]), runner, now=now)]

    root = opts.out_dir or Path(".danno-bench") / now.strftime("%Y-%m-%dT%H-%M-%S")
    log_info(f"danno bench — {len(harnesses)} harnesses [{', '.join(harnesses)}] → {root}")
    reports = [
        run_bench(config, bench_cfg, replace(opts, harness=ag, out_dir=root / ag), runner, now=now)
        for ag in harnesses
    ]
    if opts.dry_run:
        return reports

    jsons = [r.results_json for r in reports if r.results_json is not None]
    if jsons:
        payloads = load_reports(jsons)
        root.mkdir(parents=True, exist_ok=True)
        md = root / "report.md"
        html = root / "report.html"
        md.write_text(merge_markdown(payloads), encoding="utf-8")
        html.write_text(merge_html(payloads), encoding="utf-8")
        log_info(f"\n  comparison  {md}  ·  {html}")
    return reports
