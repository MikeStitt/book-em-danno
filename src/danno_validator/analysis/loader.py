"""Compatibility boundary from current and reasonable historical bench artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from danno_validator.analysis.config import AnalysisConfig, PricingConfig
from danno_validator.analysis.models import Observation


def _digest(value: object, *, prefix: str) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:16]}"


def discover_bench_files(inputs: list[Path]) -> list[Path]:
    """Resolve files, run dirs, and multi-harness roots without broad recursive guessing."""
    found: set[Path] = set()
    for raw in inputs:
        path = raw.resolve()
        if path.is_file():
            if path.name != "bench.json":
                raise ValueError(f"analysis input file must be named bench.json: {path}")
            found.add(path)
            continue
        if not path.is_dir():
            raise ValueError(f"analysis run not found: {path}")
        direct = path / "bench.json"
        if direct.is_file():
            found.add(direct)
            continue
        children = sorted(path.glob("*/bench.json"))
        if not children:
            raise ValueError(f"no bench.json found in run directory or immediate children: {path}")
        found.update(child.resolve() for child in children)
    if not found:
        raise ValueError("at least one completed bench run is required")
    return sorted(found)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: malformed JSON — {exc}") from exc
    except OSError as exc:
        raise ValueError(f"{path}: cannot read artifact — {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _object_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _relative_sidecar(run_dir: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else run_dir / path


def _failure_class(row: dict[str, Any]) -> str:
    if row.get("passed") is True:
        return "pass"
    gate = row.get("gate")
    if isinstance(gate, dict):
        name = str(gate.get("gate") or "")
        if "token" in name:
            return "token_budget_breach"
        if "turn" in name or "round" in name:
            return "turn_limit_runaway"
        if "timeout" in name:
            return "timeout"
        return "gate_breach"
    termination = str(row.get("termination") or "").lower()
    error = str(row.get("error") or "").lower()
    verdict = str(row.get("verdict") or "").lower()
    combined = " ".join((termination, error, verdict))
    if "timeout" in combined:
        return "timeout"
    if "auth" in combined or "unauthorized" in combined or "provider" in combined:
        return "authentication_or_provider_error"
    if "sandbox" in combined or "docker" in combined or "provision" in combined:
        return "infrastructure_or_sandbox_failure"
    if "unsupported" in combined or "model not found" in combined:
        return "unsupported_harness_model"
    if "early_stop" in combined or "early-stop" in combined:
        return "early_stop"
    if "error" in combined:
        return "error"
    if verdict and "pass" not in verdict:
        return verdict.removeprefix("failureclass.").replace("-", "_")
    return "test_failure" if row.get("passed") is False else "unknown_failure"


def _task_categories(config: AnalysisConfig) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for category, category_config in config.categories.items():
        for task in category_config.tasks:
            if task in mapping:
                raise ValueError(
                    f"task {task!r} appears in multiple categories: "
                    f"{mapping[task]!r} and {category!r}"
                )
            mapping[task] = category
    return mapping


def _cost(
    model: str,
    input_tokens: int | None,
    cached_tokens: int | None,
    output_tokens: int | None,
    pricing: PricingConfig | None,
) -> float | None:
    if pricing is None or input_tokens is None or cached_tokens is None or output_tokens is None:
        return None
    price = pricing.models.get(model)
    if price is None:
        return None
    cached = cached_tokens
    if cached < 0 or cached > input_tokens:
        return None
    if cached and price.cached_input_per_million is None:
        return None
    uncached = input_tokens - cached
    cached_rate = price.cached_input_per_million or 0.0
    return (
        uncached * price.input_per_million
        + cached * cached_rate
        + output_tokens * price.output_per_million
    ) / 1_000_000


def load_observations(
    inputs: list[Path], config: AnalysisConfig, pricing: PricingConfig | None
) -> tuple[list[Observation], list[Path]]:
    categories = _task_categories(config)
    observations: list[Observation] = []
    bench_files = discover_bench_files(inputs)
    for bench_path in bench_files:
        payload = _read_json(bench_path)
        rows = payload.get("results")
        harness = payload.get("harness")
        if not isinstance(rows, list) or not isinstance(harness, str) or not harness:
            raise ValueError(f"{bench_path}: requires string 'harness' and array 'results'")
        run_dir = bench_path.parent
        provenance_path = run_dir / "provenance.json"
        provenance = _read_json(provenance_path) if provenance_path.is_file() else {}
        danno = _object_dict(provenance.get("danno"))
        host = _object_dict(provenance.get("host"))
        harness_prov = _object_dict(provenance.get("harness_versions"))
        model_prov = _object_dict(provenance.get("models"))
        cohort_basis = {
            "danno": danno,
            "host": host,
            "sample_interval_s": provenance.get("sample_interval_s"),
        }
        if not danno or not host:
            # Unknown provenance is not evidence that two historical runs match.
            cohort_basis["unknown_source"] = str(run_dir)
        cohort_id = _digest(cohort_basis, prefix="cohort")
        run_id = _digest(
            {"path": str(run_dir), "generated_at": payload.get("generated_at")},
            prefix="run",
        )
        for index, raw_row in enumerate(rows):
            if not isinstance(raw_row, dict):
                raise ValueError(f"{bench_path}: results[{index}] must be an object")
            task = raw_row.get("task")
            suite = raw_row.get("suite")
            passed = raw_row.get("passed")
            if (
                not isinstance(task, str)
                or not isinstance(suite, str)
                or not isinstance(passed, bool)
            ):
                raise ValueError(
                    f"{bench_path}: results[{index}] requires string suite/task and boolean passed"
                )
            model = raw_row.get("model")
            if not isinstance(model, str) or not model:
                models = payload.get("models")
                if isinstance(models, list) and len(models) == 1 and isinstance(models[0], str):
                    model = models[0]
                else:
                    raise ValueError(f"{bench_path}: results[{index}] has no interpretable model")
            wire = _object_dict(raw_row.get("wire"))
            resource = _object_dict(raw_row.get("resource"))
            gate = _object_dict(raw_row.get("gate"))
            sidecars = _object_dict(raw_row.get("sidecars"))
            input_tokens = _optional_int(wire.get("input_tokens"))
            cached_tokens = _optional_int(wire.get("cached_tokens"))
            output_tokens = _optional_int(wire.get("output_tokens"))
            total_tokens = _optional_int(wire.get("total_tokens"))
            if total_tokens is None:
                total_tokens = _optional_int(raw_row.get("tokens"))
            model_facts = _object_dict(model_prov.get(model))
            variant_basis = {
                "harness": harness,
                "model": model,
                "model_provenance": model_facts,
                "harness_provenance": harness_prov,
                "gates": provenance.get("gates"),
                "config_path": payload.get("config"),
            }
            variant_id = _digest(variant_basis, prefix="variant")
            configuration_id = _digest(
                {"variant": variant_id, "cohort": cohort_id}, prefix="config"
            )
            captures_value = sidecars.get("captures")
            capture_paths = tuple(
                path
                for value in (captures_value if isinstance(captures_value, list) else [])
                if (path := _relative_sidecar(run_dir, value)) is not None
            )
            observation = Observation(
                run_id=run_id,
                source_dir=run_dir,
                generated_at=(
                    str(payload["generated_at"])
                    if payload.get("generated_at") is not None
                    else None
                ),
                danno_version=str(danno["version"]) if danno.get("version") else None,
                danno_commit=str(danno["commit"]) if danno.get("commit") else None,
                harness=harness,
                model=model,
                backend_identity=(
                    str(model_facts.get("digest")) if model_facts.get("digest") else None
                ),
                sandbox_identity=(
                    str(harness_prov.get("image")) if harness_prov.get("image") else None
                ),
                environment_identity=cohort_id,
                suite=suite,
                task=task,
                category=categories.get(task) or categories.get(f"{suite}/{task}"),
                attempt_id=f"{run_id}:{index}",
                passed=passed,
                verdict=str(raw_row["verdict"]) if raw_row.get("verdict") is not None else None,
                failure_class=_failure_class(raw_row),
                error_summary=(str(raw_row["error"]) if raw_row.get("error") is not None else None),
                termination_reason=(
                    str(raw_row["termination"]) if raw_row.get("termination") is not None else None
                ),
                gate_breached=str(gate["gate"]) if gate.get("gate") is not None else None,
                turns=_optional_int(raw_row.get("rounds")),
                input_tokens=input_tokens,
                cached_input_tokens=cached_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
                latency_s=_optional_float(raw_row.get("latency_s")),
                ttft_s=_optional_float(wire.get("ttft_s")),
                rtt_min_s=_optional_float(wire.get("rtt_min_s")),
                rtt_mean_s=_optional_float(wire.get("rtt_mean_s")),
                rtt_max_s=_optional_float(wire.get("rtt_max_s")),
                peak_context_tokens=_optional_int(wire.get("peak_ctx_tokens")),
                context_headroom_pct=_optional_float(wire.get("ctx_headroom_pct")),
                cpu_peak_pct=_optional_float(resource.get("cpu_peak")),
                ram_peak_kb=_optional_int(resource.get("mem_peak_kb")),
                gpu_peak_pct=_optional_float(resource.get("gpu_util_peak")),
                vram_peak_mb=_optional_float(resource.get("vram_peak_mb")),
                capture_paths=capture_paths,
                transcript_path=_relative_sidecar(run_dir, sidecars.get("transcript")),
                metrics_path=_relative_sidecar(run_dir, sidecars.get("metrics")),
                samples_path=_relative_sidecar(run_dir, sidecars.get("samples")),
                provenance=provenance,
                variant_id=variant_id,
                cohort_id=cohort_id,
                configuration_id=configuration_id,
            )
            observations.append(
                replace(
                    observation,
                    estimated_cost=_cost(
                        model,
                        observation.input_tokens,
                        observation.cached_input_tokens,
                        observation.output_tokens,
                        pricing,
                    ),
                )
            )
    if not observations:
        raise ValueError("completed bench artifacts contain no result observations")
    return observations, [path.parent for path in bench_files]
