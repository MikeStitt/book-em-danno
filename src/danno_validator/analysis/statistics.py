"""Deterministic statistics with repository tasks as the sampling unit."""

from __future__ import annotations

import hashlib
import json
import math
import random
import statistics
from collections.abc import Iterable, Mapping
from statistics import NormalDist

BOOTSTRAP_RESAMPLES = 10_000
TASK_BOOTSTRAP_METHOD = "task_cluster_percentile_bootstrap"


def complete_values(values: Iterable[int | float | None], expected: int) -> list[float] | None:
    present = [float(value) for value in values if value is not None]
    return present if len(present) == expected else None


def mean(values: list[float] | None) -> float | None:
    return statistics.fmean(values) if values else None


def median(values: list[float] | None) -> float | None:
    return statistics.median(values) if values else None


def sample_variance(values: list[float] | None) -> float | None:
    return statistics.variance(values) if values is not None and len(values) >= 2 else None


def p95(values: list[float] | None) -> float | None:
    """Nearest-rank p95; undefined for a single observation."""
    if values is None or len(values) < 2:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def wilson_interval(passed: int, total: int, confidence: float) -> tuple[float, float]:
    """Pooled-observation IID diagnostic, not the deployment interval."""
    if total <= 0:
        return (0.0, 1.0)
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    phat = passed / total
    denominator = 1 + z * z / total
    centre = (phat + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(phat * (1 - phat) / total + z * z / (4 * total * total)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def stable_scope_seed(
    configured_seed: int,
    *,
    configuration_id: str,
    scope: str,
) -> int:
    """A process-stable seed; Python's randomized ``hash()`` is never involved."""
    value = json.dumps(
        {
            "configured_seed": configured_seed,
            "configuration_id": configuration_id,
            "scope": scope,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return int.from_bytes(hashlib.sha256(value).digest()[:8], "big")


def _percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires at least one value")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def task_cluster_bootstrap(
    task_outcomes: Mapping[str, Iterable[bool]],
    *,
    confidence: float,
    seed: int,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> tuple[float, tuple[float, float]]:
    """Mean task pass rate and percentile interval from task-cluster resampling.

    Each canonical task contributes one empirical pass rate, regardless of how many
    repetitions it has. Bootstrap samples draw those task rates with replacement.
    """
    if resamples < 1:
        raise ValueError("bootstrap resamples must be positive")
    task_rates: list[float] = []
    for task in sorted(task_outcomes):
        outcomes = tuple(bool(value) for value in task_outcomes[task])
        if not outcomes:
            continue
        task_rates.append(sum(outcomes) / len(outcomes))
    if not task_rates:
        return 0.0, (0.0, 1.0)
    point = statistics.fmean(task_rates)
    if len(task_rates) == 1:
        return point, (point, point)
    rng = random.Random(seed)
    cluster_count = len(task_rates)
    draws = [
        statistics.fmean(task_rates[rng.randrange(cluster_count)] for _ in range(cluster_count))
        for _ in range(resamples)
    ]
    alpha = (1 - confidence) / 2
    return point, (_percentile(draws, alpha), _percentile(draws, 1 - alpha))
