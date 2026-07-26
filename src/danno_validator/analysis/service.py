"""Application service composing the pure analysis stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from danno_validator.analysis.config import load_analysis_config, load_pricing_config
from danno_validator.analysis.engine import (
    build_aggregates,
    build_recommendations,
    comparability_warnings,
    pareto_analysis,
)
from danno_validator.analysis.loader import load_observations
from danno_validator.analysis.models import Study
from danno_validator.analysis.render import write_outputs


@dataclass(frozen=True)
class AnalysisResult:
    study: Study
    recommendation_json: Path
    markdown_report: Path
    html_report: Path


def analyze_runs(
    runs: list[Path],
    *,
    out_dir: Path,
    config_path: Path | None = None,
    pricing_path: Path | None = None,
    now: datetime | None = None,
) -> AnalysisResult:
    config = load_analysis_config(config_path)
    pricing = load_pricing_config(pricing_path)
    observations, sources = load_observations(runs, config, pricing)
    aggregates = build_aggregates(observations, config)
    recommendations = build_recommendations(aggregates, config)
    pareto = pareto_analysis(aggregates, config.recommendation.reliability_threshold)
    generated = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    study = Study(
        name=config.study,
        generated_at=generated,
        observations=tuple(observations),
        aggregates=tuple(aggregates),
        recommendations=tuple(recommendations),
        pareto=pareto,
        comparability_warnings=tuple(comparability_warnings(observations)),
        source_runs=tuple(sources),
        confidence_level=config.confidence_level,
        statistics_seed=config.statistics_seed,
        currency=pricing.currency if pricing else None,
        pricing_effective_date=pricing.effective_date if pricing else None,
        methodology={
            "deployment_success_interval": "task_cluster_percentile_bootstrap",
            "sampling_unit": "canonical_repository_task",
            "bootstrap_resamples": 10_000,
            "confidence_level": config.confidence_level,
            "reliability_eligibility_basis": config.recommendation.reliability_basis,
            "minimum_distinct_tasks": config.recommendation.minimum_distinct_tasks,
            "p95": "nearest_rank_minimum_two_observations",
            "statistics_seed": config.statistics_seed,
            "scope_seed": "sha256(configured_seed, configuration_id, scope)",
            "iid_success_interval": "wilson_score_diagnostic_only",
            "variance": {
                "overall": "sample_variance_across_all_observations",
                "within_task": "median_of_per_task_sample_variances",
            },
            "pass_at_k": {
                "status": "not_reported",
                "reason": "current artifacts do not establish independent repeated sampling",
            },
            "tie_breaking": [
                "hard_constraints",
                config.recommendation.objective,
                "reliability_lower_bound",
                "cost_per_success",
                "p95_latency",
                "median_within_task_latency_variance",
                "configuration_id",
            ],
        },
    )
    recommendation, markdown, html = write_outputs(study, out_dir.resolve())
    return AnalysisResult(
        study=study,
        recommendation_json=recommendation,
        markdown_report=markdown,
        html_report=html,
    )
