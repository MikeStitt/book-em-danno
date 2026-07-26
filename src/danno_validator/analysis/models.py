"""Typed, execution-independent models used by the analysis layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

RecommendationStatus = Literal[
    "recommended",
    "insufficient_evidence",
    "no_configuration_satisfies_constraints",
    "not_comparable",
]


@dataclass(frozen=True)
class Observation:
    """One normalized benchmark cell.

    Optional telemetry remains ``None`` when the source did not measure it. In
    particular, this model never turns an absent historical field into numeric zero.
    """

    run_id: str
    source_dir: Path
    generated_at: str | None
    danno_version: str | None
    danno_commit: str | None
    harness: str
    model: str
    backend_identity: str | None
    sandbox_identity: str | None
    environment_identity: str
    suite: str
    task: str
    category: str | None
    attempt_id: str
    passed: bool
    verdict: str | None
    failure_class: str
    error_summary: str | None
    termination_reason: str | None
    gate_breached: str | None
    turns: int | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    latency_s: float | None
    ttft_s: float | None
    rtt_min_s: float | None
    rtt_mean_s: float | None
    rtt_max_s: float | None
    peak_context_tokens: int | None
    context_headroom_pct: float | None
    cpu_peak_pct: float | None
    ram_peak_kb: int | None
    gpu_peak_pct: float | None
    vram_peak_mb: float | None
    capture_paths: tuple[Path, ...]
    transcript_path: Path | None
    metrics_path: Path | None
    samples_path: Path | None
    provenance: dict[str, object]
    variant_id: str
    configuration_group: str | None
    cohort_id: str
    configuration_id: str
    estimated_cost: float | None = None


@dataclass(frozen=True)
class TaskConsistency:
    task: str
    observations: int
    passed: int
    consistency: float
    flips: bool
    token_variance: float | None
    latency_variance: float | None
    cost_variance: float | None


@dataclass(frozen=True)
class Aggregate:
    """Metrics for one comparable configuration cohort and report scope."""

    configuration_id: str
    variant_id: str
    cohort_id: str
    harness: str
    model: str
    category: str | None
    observations: int
    passed: int
    success_rate: float
    deployment_success_rate: float
    deployment_success_interval: tuple[float, float]
    success_interval_method: str
    bootstrap_resamples: int
    bootstrap_seed: int
    iid_success_interval: tuple[float, float]
    distinct_tasks: int
    tasks: tuple[str, ...]
    repetitions: int
    total_input_tokens: int | None
    average_input_tokens: float | None
    total_output_tokens: int | None
    average_output_tokens: float | None
    average_total_tokens: float | None
    median_total_tokens: float | None
    overall_token_variance: float | None
    average_latency_s: float | None
    median_latency_s: float | None
    p95_latency_s: float | None
    overall_latency_variance: float | None
    peak_cpu_pct: float | None
    peak_ram_kb: int | None
    peak_gpu_pct: float | None
    peak_vram_mb: float | None
    gate_breach_rate: float | None
    failure_classes: dict[str, int]
    total_cost: float | None
    cost_per_attempt: float | None
    cost_per_success: float | None
    overall_cost_variance: float | None
    median_within_task_token_variance: float | None
    median_within_task_latency_variance: float | None
    median_within_task_cost_variance: float | None
    tasks_with_repeated_measurements: int
    task_consistency: tuple[TaskConsistency, ...]
    flip_tasks: tuple[str, ...]
    evidence_quality: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Recommendation:
    scope: str
    status: RecommendationStatus
    objective: str
    constraints: dict[str, object]
    primary: Aggregate | None
    fallback: Aggregate | None
    reason: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParetoResult:
    reliability_cost: tuple[str, ...] = ()
    reliability_latency: tuple[str, ...] = ()
    cost_latency: tuple[str, ...] = ()
    dominated: tuple[str, ...] = ()


@dataclass(frozen=True)
class Study:
    name: str
    generated_at: str
    observations: tuple[Observation, ...]
    aggregates: tuple[Aggregate, ...]
    recommendations: tuple[Recommendation, ...]
    pareto: ParetoResult
    comparability_warnings: tuple[str, ...]
    source_runs: tuple[Path, ...]
    confidence_level: float
    statistics_seed: int
    currency: str | None
    pricing_effective_date: str | None
    methodology: dict[str, object] = field(default_factory=dict)
