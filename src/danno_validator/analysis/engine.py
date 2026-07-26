"""Pure aggregation, uncertainty, Pareto, and recommendation policy."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable

from danno_validator.analysis.config import AnalysisConfig, RecommendationConfig
from danno_validator.analysis.models import (
    Aggregate,
    Observation,
    ParetoResult,
    Recommendation,
    TaskConsistency,
)
from danno_validator.analysis.statistics import (
    BOOTSTRAP_RESAMPLES,
    TASK_BOOTSTRAP_METHOD,
    complete_values,
    mean,
    median,
    p95,
    sample_variance,
    stable_scope_seed,
    task_cluster_bootstrap,
    wilson_interval,
)


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
    rows: list[Observation],
    *,
    category: str | None,
    confidence: float,
    statistics_seed: int = 1729,
    bootstrap_resamples: int = BOOTSTRAP_RESAMPLES,
) -> Aggregate:
    first = rows[0]
    count = len(rows)
    passed = sum(row.passed for row in rows)
    iid_interval = wilson_interval(passed, count, confidence)
    input_values = complete_values((row.input_tokens for row in rows), count)
    output_values = complete_values((row.output_tokens for row in rows), count)
    total_values = complete_values((row.total_tokens for row in rows), count)
    latency_values = complete_values((row.latency_s for row in rows), count)
    cost_values = complete_values((row.estimated_cost for row in rows), count)
    cpu_values = complete_values((row.cpu_peak_pct for row in rows), count)
    ram_values = complete_values((row.ram_peak_kb for row in rows), count)
    gpu_values = complete_values((row.gpu_peak_pct for row in rows), count)
    vram_values = complete_values((row.vram_peak_mb for row in rows), count)
    task_rows: dict[str, list[Observation]] = defaultdict(list)
    for row in rows:
        task_rows[f"{row.suite}/{row.task}"].append(row)
    scope_seed = stable_scope_seed(
        statistics_seed,
        configuration_id=first.configuration_id,
        scope=category or "overall",
    )
    deployment_rate, deployment_interval = task_cluster_bootstrap(
        {task: [attempt.passed for attempt in attempts] for task, attempts in task_rows.items()},
        confidence=confidence,
        seed=scope_seed,
        resamples=bootstrap_resamples,
    )
    consistency: list[TaskConsistency] = []
    for task, attempts in sorted(task_rows.items()):
        task_passed = sum(row.passed for row in attempts)
        task_tokens = complete_values((row.total_tokens for row in attempts), len(attempts))
        task_latency = complete_values((row.latency_s for row in attempts), len(attempts))
        task_cost = complete_values((row.estimated_cost for row in attempts), len(attempts))
        consistency.append(
            TaskConsistency(
                task=task,
                observations=len(attempts),
                passed=task_passed,
                consistency=max(task_passed, len(attempts) - task_passed) / len(attempts),
                flips=0 < task_passed < len(attempts),
                token_variance=sample_variance(task_tokens),
                latency_variance=sample_variance(task_latency),
                cost_variance=sample_variance(task_cost),
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
        deployment_success_rate=deployment_rate,
        deployment_success_interval=deployment_interval,
        success_interval_method=TASK_BOOTSTRAP_METHOD,
        confidence_level=confidence,
        bootstrap_resamples=bootstrap_resamples,
        bootstrap_seed=scope_seed,
        iid_success_interval=iid_interval,
        distinct_tasks=len(task_rows),
        tasks=tuple(sorted(task_rows)),
        repetitions=max(len(attempts) for attempts in task_rows.values()),
        total_input_tokens=int(sum(input_values)) if input_values is not None else None,
        average_input_tokens=mean(input_values),
        total_output_tokens=int(sum(output_values)) if output_values is not None else None,
        average_output_tokens=mean(output_values),
        average_total_tokens=mean(total_values),
        median_total_tokens=median(total_values),
        overall_token_variance=sample_variance(total_values),
        average_latency_s=mean(latency_values),
        median_latency_s=median(latency_values),
        p95_latency_s=p95(latency_values),
        overall_latency_variance=sample_variance(latency_values),
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
        overall_cost_variance=sample_variance(cost_values),
        median_within_task_token_variance=median(
            [item.token_variance for item in consistency if item.token_variance is not None] or None
        ),
        median_within_task_latency_variance=median(
            [item.latency_variance for item in consistency if item.latency_variance is not None]
            or None
        ),
        median_within_task_cost_variance=median(
            [item.cost_variance for item in consistency if item.cost_variance is not None] or None
        ),
        tasks_with_repeated_measurements=sum(item.observations >= 2 for item in consistency),
        task_consistency=tuple(consistency),
        flip_tasks=tuple(item.task for item in consistency if item.flips),
        evidence_quality=_quality(count, len(task_rows), deployment_interval),
        warnings=tuple(warnings),
    )


def build_aggregates(observations: list[Observation], config: AnalysisConfig) -> list[Aggregate]:
    groups: dict[tuple[str, str | None], list[Observation]] = defaultdict(list)
    for row in observations:
        groups[(row.configuration_id, None)].append(row)
        if row.category is not None:
            groups[(row.configuration_id, row.category)].append(row)
    return [
        aggregate_group(
            rows,
            category=category,
            confidence=config.confidence_level,
            statistics_seed=config.statistics_seed,
        )
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
        "reliability_basis": policy.reliability_basis,
        "maximum_cost_per_attempt": policy.maximum_cost_per_attempt,
        "maximum_cost_per_success": policy.maximum_cost_per_success,
        "maximum_p95_latency_s": policy.maximum_p95_latency_s,
        "allowed_harnesses": policy.allowed_harnesses,
        "allowed_models": policy.allowed_models,
        "minimum_sample_count": policy.minimum_sample_count,
        "minimum_distinct_tasks": policy.minimum_distinct_tasks,
    }


def _reliability_value(aggregate: Aggregate, policy: RecommendationConfig) -> float:
    if policy.reliability_basis == "lower_confidence_bound":
        return aggregate.deployment_success_interval[0]
    return aggregate.deployment_success_rate


def _eligible(aggregate: Aggregate, policy: RecommendationConfig) -> bool:
    if aggregate.observations < policy.minimum_sample_count:
        return False
    if aggregate.distinct_tasks < policy.minimum_distinct_tasks:
        return False
    if _reliability_value(aggregate, policy) < policy.reliability_threshold:
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
    lower_reliability = aggregate.deployment_success_interval[0]
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
        _ascending(aggregate.median_within_task_latency_variance),
        aggregate.configuration_id,
    )


def _recommendation_reason(
    primary: Aggregate,
    fallback: Aggregate | None,
    policy: RecommendationConfig,
) -> str:
    reliability = _reliability_value(primary, policy)
    basis = (
        f"{primary.confidence_level:.0%} task-bootstrap reliability lower bound"
        if policy.reliability_basis == "lower_confidence_bound"
        else "task-weighted reliability point estimate"
    )
    clauses = [
        f"Selected {primary.harness} / {primary.model} because its {basis} was "
        f"{reliability:.3f} across {primary.distinct_tasks} tasks and "
        f"{primary.observations} observations, satisfying the "
        f"{policy.reliability_threshold:.3f} requirement."
    ]
    if policy.objective == "lowest_cost" and primary.cost_per_success is not None:
        clauses.append(
            f"It had the lowest cost per successful task "
            f"({primary.cost_per_success:.6g}) among eligible configurations."
        )
    elif policy.objective == "lowest_latency" and primary.p95_latency_s is not None:
        clauses.append(
            f"It had the lowest p95 latency ({primary.p95_latency_s:.6g}s) "
            "among eligible configurations."
        )
    else:
        clauses.append(
            "It had the highest task-bootstrap reliability lower bound among "
            "eligible configurations."
        )
    if fallback is not None:
        fallback_name = f"{fallback.harness} / {fallback.model}"
        if policy.objective == "lowest_cost" and fallback.cost_per_success is not None:
            clauses.append(
                f"It beat fallback {fallback_name}, whose cost per successful task was "
                f"{fallback.cost_per_success:.6g}."
            )
        elif policy.objective == "lowest_latency" and fallback.p95_latency_s is not None:
            clauses.append(
                f"It beat fallback {fallback_name}, whose p95 latency was "
                f"{fallback.p95_latency_s:.6g}s."
            )
        else:
            clauses.append(
                f"It beat fallback {fallback_name}, whose task-bootstrap reliability "
                f"lower bound was {fallback.deployment_success_interval[0]:.3f}."
            )
    clauses.append(f"Evidence quality is {primary.evidence_quality}.")
    return " ".join(clauses)


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
    enough_tasks = [
        item for item in aggregates if item.distinct_tasks >= policy.minimum_distinct_tasks
    ]
    if not enough_tasks:
        return Recommendation(
            scope=scope,
            status="insufficient_evidence",
            objective=policy.objective,
            constraints=constraints,
            primary=None,
            fallback=None,
            reason=(
                "No configuration has evidence across the required "
                f"{policy.minimum_distinct_tasks} distinct tasks."
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
    reason = _recommendation_reason(primary, fallback, policy)
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
