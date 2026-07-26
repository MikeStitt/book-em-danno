"""Pure aggregation, uncertainty, Pareto, and recommendation policy."""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from statistics import NormalDist

from danno_validator.analysis.config import AnalysisConfig, RecommendationConfig
from danno_validator.analysis.models import (
    Aggregate,
    Observation,
    ParetoResult,
    Recommendation,
    TaskConsistency,
)


def _complete(values: Iterable[int | float | None], expected: int) -> list[float] | None:
    present = [float(value) for value in values if value is not None]
    return present if len(present) == expected else None


def _mean(values: list[float] | None) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float] | None) -> float | None:
    return statistics.median(values) if values else None


def _variance(values: list[float] | None) -> float | None:
    return statistics.variance(values) if values is not None and len(values) >= 2 else None


def _p95(values: list[float] | None) -> float | None:
    """Nearest-rank p95; undefined for a single observation."""
    if values is None or len(values) < 2:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def wilson_interval(passed: int, total: int, confidence: float) -> tuple[float, float]:
    """Wilson score interval for a Bernoulli success proportion."""
    if total <= 0:
        return (0.0, 1.0)
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    phat = passed / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def _quality(observations: int, distinct_tasks: int, interval: tuple[float, float]) -> str:
    width = interval[1] - interval[0]
    if observations < 2 or distinct_tasks < 2:
        return "insufficient"
    if observations >= 20 and distinct_tasks >= 10 and width <= 0.25:
        return "strong"
    if observations >= 8 and distinct_tasks >= 4 and width <= 0.45:
        return "moderate"
    return "weak"


def aggregate_group(
    rows: list[Observation], *, category: str | None, confidence: float
) -> Aggregate:
    first = rows[0]
    count = len(rows)
    passed = sum(row.passed for row in rows)
    interval = wilson_interval(passed, count, confidence)
    input_values = _complete((row.input_tokens for row in rows), count)
    output_values = _complete((row.output_tokens for row in rows), count)
    total_values = _complete((row.total_tokens for row in rows), count)
    latency_values = _complete((row.latency_s for row in rows), count)
    cost_values = _complete((row.estimated_cost for row in rows), count)
    cpu_values = _complete((row.cpu_peak_pct for row in rows), count)
    ram_values = _complete((row.ram_peak_kb for row in rows), count)
    gpu_values = _complete((row.gpu_peak_pct for row in rows), count)
    vram_values = _complete((row.vram_peak_mb for row in rows), count)
    task_rows: dict[str, list[Observation]] = defaultdict(list)
    for row in rows:
        task_rows[f"{row.suite}/{row.task}"].append(row)
    consistency: list[TaskConsistency] = []
    for task, attempts in sorted(task_rows.items()):
        task_passed = sum(row.passed for row in attempts)
        consistency.append(
            TaskConsistency(
                task=task,
                observations=len(attempts),
                passed=task_passed,
                consistency=max(task_passed, len(attempts) - task_passed) / len(attempts),
                flips=0 < task_passed < len(attempts),
            )
        )
    warnings: list[str] = []
    telemetry = (
        ("input token", input_values),
        ("output token", output_values),
        ("total token", total_values),
        ("latency", latency_values),
    )
    warnings.extend(
        f"{label} aggregates unavailable because one or more observations are missing"
        for label, values in telemetry
        if values is None
    )
    if cost_values is None:
        warnings.append("cost unavailable for one or more observations")
    terminations_known = all(row.termination_reason is not None for row in rows)
    gate_rate = (
        sum(row.gate_breached is not None for row in rows) / count if terminations_known else None
    )
    if not terminations_known:
        warnings.append("gate-breach rate unavailable for historical rows without termination data")
    total_cost = sum(cost_values) if cost_values is not None else None
    return Aggregate(
        configuration_id=first.configuration_id,
        variant_id=first.variant_id,
        cohort_id=first.cohort_id,
        harness=first.harness,
        model=first.model,
        category=category,
        observations=count,
        passed=passed,
        success_rate=passed / count,
        success_interval=interval,
        distinct_tasks=len(task_rows),
        tasks=tuple(sorted(task_rows)),
        repetitions=max(len(attempts) for attempts in task_rows.values()),
        total_input_tokens=int(sum(input_values)) if input_values is not None else None,
        average_input_tokens=_mean(input_values),
        total_output_tokens=int(sum(output_values)) if output_values is not None else None,
        average_output_tokens=_mean(output_values),
        average_total_tokens=_mean(total_values),
        median_total_tokens=_median(total_values),
        token_variance=_variance(total_values),
        average_latency_s=_mean(latency_values),
        median_latency_s=_median(latency_values),
        p95_latency_s=_p95(latency_values),
        latency_variance=_variance(latency_values),
        peak_cpu_pct=max(cpu_values) if cpu_values is not None else None,
        peak_ram_kb=int(max(ram_values)) if ram_values is not None else None,
        peak_gpu_pct=max(gpu_values) if gpu_values is not None else None,
        peak_vram_mb=max(vram_values) if vram_values is not None else None,
        gate_breach_rate=gate_rate,
        failure_classes=dict(
            sorted(Counter(row.failure_class for row in rows if not row.passed).items())
        ),
        total_cost=total_cost,
        cost_per_attempt=total_cost / count if total_cost is not None else None,
        cost_per_success=(total_cost / passed if total_cost is not None and passed > 0 else None),
        cost_variance=_variance(cost_values),
        task_consistency=tuple(consistency),
        flip_tasks=tuple(item.task for item in consistency if item.flips),
        evidence_quality=_quality(count, len(task_rows), interval),
        warnings=tuple(warnings),
    )


def build_aggregates(observations: list[Observation], config: AnalysisConfig) -> list[Aggregate]:
    groups: dict[tuple[str, str | None], list[Observation]] = defaultdict(list)
    for row in observations:
        groups[(row.configuration_id, None)].append(row)
        if row.category is not None:
            groups[(row.configuration_id, row.category)].append(row)
    return [
        aggregate_group(rows, category=category, confidence=config.confidence_level)
        for (_, category), rows in sorted(
            groups.items(),
            key=lambda item: (
                item[0][0],
                item[0][1] is not None,
                item[0][1] or "",
            ),
        )
    ]


def comparability_warnings(observations: list[Observation]) -> list[str]:
    warnings: list[str] = []
    source_runs = {row.source_dir for row in observations}
    if len(source_runs) > 1 and any(row.configuration_group is None for row in observations):
        warnings.append(
            "Cross-run configuration equivalence was not declared and cannot be verified "
            "from current artifacts; source runs remain separate configurations."
        )
    cohorts = {row.cohort_id for row in observations}
    if len(cohorts) > 1:
        warnings.append(
            f"{len(cohorts)} execution cohorts differ in Danno revision or host environment; "
            "they are reported separately and not ranked against each other"
        )
    by_variant: dict[str, set[str]] = defaultdict(set)
    for row in observations:
        by_variant[row.variant_id].add(row.cohort_id)
    for variant, variant_cohorts in sorted(by_variant.items()):
        if len(variant_cohorts) > 1:
            warnings.append(f"{variant} appears in {len(variant_cohorts)} non-comparable cohorts")
    if any(row.sandbox_identity is None for row in observations):
        warnings.append("current run artifacts do not identify the sandbox image or policy")
    if any(row.backend_identity is None for row in observations):
        warnings.append("some model/backend identities lack an immutable digest")
    warnings.append(
        "attempt identities are derived from source run and row order because bench.json "
        "does not record an explicit repetition id"
    )
    return warnings


def _frontier(
    aggregates: list[Aggregate],
    dimensions: tuple[tuple[Callable[[Aggregate], float | None], bool], ...],
) -> tuple[tuple[str, ...], set[str]]:
    eligible = [
        aggregate
        for aggregate in aggregates
        if all(accessor(aggregate) is not None for accessor, _ in dimensions)
    ]

    def no_worse(left: Aggregate, right: Aggregate) -> bool:
        for accessor, maximize in dimensions:
            left_value = accessor(left)
            right_value = accessor(right)
            if left_value is None or right_value is None:
                return False
            if maximize and left_value < right_value:
                return False
            if not maximize and left_value > right_value:
                return False
        return True

    def strictly_better(left: Aggregate, right: Aggregate) -> bool:
        for accessor, maximize in dimensions:
            left_value = accessor(left)
            right_value = accessor(right)
            if left_value is None or right_value is None:
                continue
            if maximize and left_value > right_value:
                return True
            if not maximize and left_value < right_value:
                return True
        return False

    dominated: set[str] = set()
    for candidate in eligible:
        if any(
            other.configuration_id != candidate.configuration_id
            and no_worse(other, candidate)
            and strictly_better(other, candidate)
            for other in eligible
        ):
            dominated.add(candidate.configuration_id)
    frontier = tuple(
        sorted(
            aggregate.configuration_id
            for aggregate in eligible
            if aggregate.configuration_id not in dominated
        )
    )
    return frontier, dominated


def pareto_analysis(aggregates: list[Aggregate], reliability_threshold: float) -> ParetoResult:
    overall = [aggregate for aggregate in aggregates if aggregate.category is None]
    if len({item.cohort_id for item in overall}) > 1 or len({item.tasks for item in overall}) > 1:
        return ParetoResult()
    reliability_cost, dominated_cost = _frontier(
        overall,
        (
            (lambda item: item.success_rate, True),
            (lambda item: item.cost_per_attempt, False),
        ),
    )
    reliability_latency, dominated_latency = _frontier(
        overall,
        (
            (lambda item: item.success_rate, True),
            (lambda item: item.p95_latency_s, False),
        ),
    )
    threshold = [item for item in overall if item.success_rate >= reliability_threshold]
    cost_latency, dominated_cost_latency = _frontier(
        threshold,
        (
            (lambda item: item.cost_per_attempt, False),
            (lambda item: item.p95_latency_s, False),
        ),
    )
    return ParetoResult(
        reliability_cost=reliability_cost,
        reliability_latency=reliability_latency,
        cost_latency=cost_latency,
        dominated=tuple(sorted(dominated_cost | dominated_latency | dominated_cost_latency)),
    )


def _constraints_dict(policy: RecommendationConfig) -> dict[str, object]:
    return {
        "minimum_success_rate": policy.reliability_threshold,
        "maximum_cost_per_attempt": policy.maximum_cost_per_attempt,
        "maximum_cost_per_success": policy.maximum_cost_per_success,
        "maximum_p95_latency_s": policy.maximum_p95_latency_s,
        "allowed_harnesses": policy.allowed_harnesses,
        "allowed_models": policy.allowed_models,
        "minimum_sample_count": policy.minimum_sample_count,
    }


def _eligible(aggregate: Aggregate, policy: RecommendationConfig) -> bool:
    if aggregate.observations < policy.minimum_sample_count:
        return False
    if aggregate.success_rate < policy.reliability_threshold:
        return False
    if policy.allowed_harnesses is not None and aggregate.harness not in policy.allowed_harnesses:
        return False
    if policy.allowed_models is not None and aggregate.model not in policy.allowed_models:
        return False
    if policy.maximum_cost_per_attempt is not None and (
        aggregate.cost_per_attempt is None
        or aggregate.cost_per_attempt > policy.maximum_cost_per_attempt
    ):
        return False
    if policy.maximum_cost_per_success is not None and (
        aggregate.cost_per_success is None
        or aggregate.cost_per_success > policy.maximum_cost_per_success
    ):
        return False
    if policy.maximum_p95_latency_s is not None and (
        aggregate.p95_latency_s is None or aggregate.p95_latency_s > policy.maximum_p95_latency_s
    ):
        return False
    if policy.objective == "lowest_cost" and aggregate.cost_per_success is None:
        return False
    if policy.objective == "lowest_latency" and aggregate.p95_latency_s is None:
        return False
    return True


def _ascending(value: float | None) -> float:
    return value if value is not None else math.inf


def _rank_key(aggregate: Aggregate, objective: str) -> tuple[object, ...]:
    lower_reliability = aggregate.success_interval[0]
    if objective == "lowest_cost":
        leading: tuple[object, ...] = (
            _ascending(aggregate.cost_per_success),
            -lower_reliability,
        )
    elif objective == "lowest_latency":
        leading = (_ascending(aggregate.p95_latency_s), -lower_reliability)
    else:
        leading = (-lower_reliability, _ascending(aggregate.cost_per_success))
    return (
        *leading,
        _ascending(aggregate.p95_latency_s),
        _ascending(aggregate.latency_variance),
        aggregate.configuration_id,
    )


def recommend_scope(
    aggregates: list[Aggregate], *, scope: str, policy: RecommendationConfig
) -> Recommendation:
    constraints = _constraints_dict(policy)
    if not aggregates:
        return Recommendation(
            scope=scope,
            status="insufficient_evidence",
            objective=policy.objective,
            constraints=constraints,
            primary=None,
            fallback=None,
            reason="No observations are mapped to this scope.",
        )
    cohorts = {aggregate.cohort_id for aggregate in aggregates}
    if len(cohorts) > 1:
        return Recommendation(
            scope=scope,
            status="not_comparable",
            objective=policy.objective,
            constraints=constraints,
            primary=None,
            fallback=None,
            reason=(
                "Candidate results span different Danno revision or host cohorts; "
                "ranking them would overstate comparability."
            ),
        )
    task_sets = {aggregate.tasks for aggregate in aggregates}
    if len(task_sets) > 1:
        return Recommendation(
            scope=scope,
            status="not_comparable",
            objective=policy.objective,
            constraints=constraints,
            primary=None,
            fallback=None,
            reason=(
                "Candidate configurations were measured on different task selections; "
                "ranking them would confound configuration quality with task coverage."
            ),
        )
    enough = [item for item in aggregates if item.observations >= policy.minimum_sample_count]
    if not enough:
        return Recommendation(
            scope=scope,
            status="insufficient_evidence",
            objective=policy.objective,
            constraints=constraints,
            primary=None,
            fallback=None,
            reason=(
                f"No configuration has the required {policy.minimum_sample_count} observations."
            ),
        )
    eligible = sorted(
        (item for item in aggregates if _eligible(item, policy)),
        key=lambda item: _rank_key(item, policy.objective),
    )
    if not eligible:
        return Recommendation(
            scope=scope,
            status="no_configuration_satisfies_constraints",
            objective=policy.objective,
            constraints=constraints,
            primary=None,
            fallback=None,
            reason="Configurations have enough evidence, but none satisfy every hard constraint.",
        )
    primary = eligible[0]
    fallback = eligible[1] if len(eligible) > 1 else None
    reason = (
        f"Selected {primary.harness} / {primary.model} using deterministic {policy.objective} "
        "ranking: hard constraints, objective, reliability lower bound, cost, p95 latency, "
        "variance, then stable configuration identity."
    )
    return Recommendation(
        scope=scope,
        status="recommended",
        objective=policy.objective,
        constraints=constraints,
        primary=primary,
        fallback=fallback,
        reason=reason,
        warnings=primary.warnings,
    )


def build_recommendations(
    aggregates: list[Aggregate], config: AnalysisConfig
) -> list[Recommendation]:
    recommendations = [
        recommend_scope(
            [item for item in aggregates if item.category is None],
            scope="overall",
            policy=config.recommendation,
        )
    ]
    for category in sorted(config.categories):
        recommendations.append(
            recommend_scope(
                [item for item in aggregates if item.category == category],
                scope=category,
                policy=config.recommendation,
            )
        )
    return recommendations
