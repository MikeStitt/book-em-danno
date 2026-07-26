"""Decision-intelligence tests over small synthetic current-schema bench runs."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from book_em_danno.cli import app
from danno_validator.analysis.config import (
    AnalysisConfig,
    RecommendationConfig,
    load_analysis_config,
)
from danno_validator.analysis.engine import (
    aggregate_group,
    build_aggregates,
    pareto_analysis,
    recommend_scope,
    wilson_interval,
)
from danno_validator.analysis.loader import load_observations
from danno_validator.analysis.service import analyze_runs
from danno_validator.analysis.statistics import (
    stable_scope_seed,
    task_cluster_bootstrap,
)


def _row(
    *,
    task: str = "python/alpha",
    model: str = "provider/model-a",
    passed: bool = True,
    latency: float = 10.0,
    input_tokens: int | None = 1_000,
    cached_tokens: int | None = 100,
    output_tokens: int | None = 200,
    verdict: str = "FailureClass.PASS",
    gate: dict[str, object] | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "suite": "aider",
        "task": task,
        "model": model,
        "passed": passed,
        "verdict": verdict,
        "termination": "gate_kill" if gate else "completed",
        "tool_calls": 3,
        "tokens": (input_tokens or 0) + (output_tokens or 0),
        "cost": 0.0,
        "latency_s": latency,
        "error": None,
        "rounds": 4,
        "resource": {
            "cpu_peak": 0.0,
            "mem_peak_kb": 0,
            "gpu_util_peak": 0.0,
            "vram_peak_mb": 0.0,
        },
        "sidecars": {
            "metrics": f"metrics/{task}.json",
            "transcript": f"transcripts/{task}.md",
            "captures": [f"captures/{task}.jsonl"],
            "samples": f"samples/{task}.jsonl",
        },
    }
    if input_tokens is not None and output_tokens is not None:
        row["wire"] = {
            "request_count": 4,
            "input_tokens": input_tokens,
            "cached_tokens": cached_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "ttft_s": 1.0,
            "rtt_min_s": 1.0,
            "rtt_mean_s": 2.0,
            "rtt_max_s": 3.0,
            "peak_ctx_tokens": 800,
            "ctx_headroom_pct": 75.0,
        }
    if gate is not None:
        row["gate"] = gate
    return row


def _write_run(
    root: Path,
    name: str,
    rows: list[dict[str, object]],
    *,
    harness: str = "opencode",
    commit: str = "abc1234",
    host: str = "test-host",
) -> Path:
    run = root / name
    run.mkdir()
    models = sorted({str(row["model"]) for row in rows})
    (run / "bench.json").write_text(
        json.dumps(
            {
                "generated_at": f"2026-07-{len(name) + 1:02d}T00:00:00Z",
                "config": "/repo/danno.toml",
                "harness": harness,
                "models": models,
                "captures_persisted": True,
                "results": rows,
            }
        ),
        encoding="utf-8",
    )
    (run / "provenance.json").write_text(
        json.dumps(
            {
                "danno": {"version": "0.16.3", "commit": commit},
                "host": {"cpu_model": host, "cpu_cores": 8},
                "harness_versions": {"harness": harness, "version": "1"},
                "gates": {"max_turns": 50, "max_tokens": 2_000_000, "timeout_s": 1800},
                "models": {
                    model: {"digest": f"sha256:{model}", "context_length": 32_000}
                    for model in models
                },
            }
        ),
        encoding="utf-8",
    )
    return run


def _write_config(
    path: Path,
    *,
    minimum: int = 2,
    run_groups: list[tuple[str, list[Path]]] | None = None,
) -> Path:
    body = (
        'schema_version = 1\nstudy = "repo-choice"\n'
        "confidence_level = 0.95\nstatistics_seed = 42\n\n"
        "[recommendation]\n"
        'objective = "reliability"\n'
        "reliability_threshold = 0.5\n"
        f"minimum_sample_count = {minimum}\n\n"
        "[categories.bug-fix]\n"
        'tasks = ["python/alpha"]\n'
    )
    for name, runs in run_groups or []:
        body += (
            "\n[[run_groups]]\n"
            f"name = {json.dumps(name)}\n"
            f"runs = [{', '.join(json.dumps(str(run)) for run in runs)}]\n"
        )
    path.write_text(body, encoding="utf-8")
    return path


def _write_pricing(path: Path) -> Path:
    path.write_text(
        'schema_version = 1\neffective_date = "2026-07-26"\ncurrency = "USD"\n\n'
        '[models."provider/model-a"]\n'
        "input_per_million = 1.0\ncached_input_per_million = 0.5\n"
        "output_per_million = 2.0\n\n"
        '[models."provider/model-b"]\n'
        "input_per_million = 4.0\ncached_input_per_million = 2.0\n"
        "output_per_million = 8.0\n",
        encoding="utf-8",
    )
    return path


def test_load_one_run_preserves_zero_missing_and_sidecars(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "one",
        [
            _row(input_tokens=0, cached_tokens=0, output_tokens=0),
            _row(task="python/beta", input_tokens=None, output_tokens=None),
        ],
    )
    observations, sources = load_observations([run], AnalysisConfig(), None)
    measured, missing = observations
    assert measured.input_tokens == 0
    assert measured.output_tokens == 0
    assert measured.cpu_peak_pct == 0.0
    assert missing.input_tokens is None
    assert missing.output_tokens is None
    assert measured.metrics_path == run / "metrics/python/alpha.json"
    assert sources == [run]
    aggregate = build_aggregates(observations, AnalysisConfig())[0]
    assert aggregate.total_input_tokens is None
    assert aggregate.average_input_tokens is None


def test_multiple_repetitions_categories_flips_and_variance(tmp_path: Path) -> None:
    first = _write_run(tmp_path, "first", [_row(passed=True, latency=10)])
    second = _write_run(
        tmp_path,
        "second",
        [_row(passed=False, latency=20, verdict="FailureClass.EARLY_STOP")],
    )
    config_path = _write_config(
        tmp_path / "analysis.toml",
        run_groups=[("repeated", [Path("first"), Path("second")])],
    )
    result = analyze_runs(
        [first, second],
        out_dir=tmp_path / "out",
        config_path=config_path,
        now=datetime(2026, 7, 26, tzinfo=UTC),
    )
    overall = next(item for item in result.study.aggregates if item.category is None)
    category = next(item for item in result.study.aggregates if item.category == "bug-fix")
    assert overall.observations == 2
    assert overall.repetitions == 2
    assert overall.flip_tasks == ("aider/python/alpha",)
    assert overall.overall_latency_variance == 50.0
    assert overall.median_within_task_latency_variance == 50.0
    assert category.observations == 2
    assert result.study.recommendations[0].status == "recommended"


def test_cross_run_configurations_are_separate_without_group(tmp_path: Path) -> None:
    first = _write_run(tmp_path, "first", [_row()])
    second = _write_run(tmp_path, "second", [_row()])
    result = analyze_runs([first, second], out_dir=tmp_path / "out")
    overall = [item for item in result.study.aggregates if item.category is None]
    assert len(overall) == 2
    assert {item.observations for item in overall} == {1}
    assert result.study.observations[0].variant_id == result.study.observations[1].variant_id
    assert (
        result.study.observations[0].configuration_id
        != result.study.observations[1].configuration_id
    )
    assert any(
        "equivalence was not declared" in warning for warning in result.study.comparability_warnings
    )


def test_explicit_relative_and_absolute_run_group_combines_repetitions(
    tmp_path: Path,
) -> None:
    first = _write_run(tmp_path, "first", [_row(passed=True)])
    second = _write_run(tmp_path, "second", [_row(passed=False)])
    config_path = _write_config(
        tmp_path / "analysis.toml",
        run_groups=[("same-config", [Path("first"), second.resolve()])],
    )
    result = analyze_runs(
        [first, second],
        out_dir=tmp_path / "out",
        config_path=config_path,
    )
    overall = [item for item in result.study.aggregates if item.category is None]
    assert len(overall) == 1
    assert overall[0].observations == 2
    assert {row.configuration_group for row in result.study.observations} == {"same-config"}


def test_duplicate_run_group_membership_fails(tmp_path: Path) -> None:
    run = _write_run(tmp_path, "run", [_row()])
    config_path = _write_config(
        tmp_path / "analysis.toml",
        run_groups=[
            ("first", [Path("run")]),
            ("second", [run.resolve()]),
        ],
    )
    with pytest.raises(ValueError, match="multiple run groups"):
        load_analysis_config(config_path)


def test_unknown_grouped_run_fails_loudly(tmp_path: Path) -> None:
    run = _write_run(tmp_path, "run", [_row()])
    config_path = _write_config(
        tmp_path / "analysis.toml",
        run_groups=[("missing", [Path("does-not-exist")])],
    )
    with pytest.raises(ValueError, match="analysis run not found"):
        analyze_runs([run], out_dir=tmp_path / "out", config_path=config_path)


def test_grouped_multi_harness_root_is_discovered(tmp_path: Path) -> None:
    root = tmp_path / "multi"
    root.mkdir()
    _write_run(root, "opencode", [_row()], harness="opencode")
    _write_run(root, "claude", [_row()], harness="claude")
    config_path = _write_config(
        tmp_path / "analysis.toml",
        run_groups=[("matrix", [Path("multi")])],
    )
    observations, sources = load_observations(
        [root],
        load_analysis_config(config_path),
        None,
    )
    assert len(observations) == 2
    assert {row.configuration_group for row in observations} == {"matrix"}
    assert sources == sorted((root / "claude", root / "opencode"))


def test_incompatible_provenance_is_separate_and_abstains(tmp_path: Path) -> None:
    old = _write_run(tmp_path, "old", [_row(), _row(task="python/beta")], commit="old")
    new = _write_run(tmp_path, "new", [_row(), _row(task="python/beta")], commit="new")
    result = analyze_runs([old, new], out_dir=tmp_path / "out")
    overall = [item for item in result.study.aggregates if item.category is None]
    assert len(overall) == 2
    assert overall[0].variant_id == overall[1].variant_id
    assert overall[0].cohort_id != overall[1].cohort_id
    assert result.study.recommendations[0].status == "not_comparable"
    assert any("execution cohorts differ" in item for item in result.study.comparability_warnings)


def test_different_task_selection_abstains_and_suppresses_pareto(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "coverage",
        [
            _row(model="provider/model-a", task="python/alpha"),
            _row(model="provider/model-a", task="python/beta"),
            _row(model="provider/model-b", task="python/alpha"),
        ],
    )
    result = analyze_runs([run], out_dir=tmp_path / "out")
    assert result.study.recommendations[0].status == "not_comparable"
    assert result.study.pareto.reliability_cost == ()
    assert "different task selections" in result.study.recommendations[0].reason


def test_pricing_cached_tokens_and_cost_per_success(tmp_path: Path) -> None:
    rows = [
        _row(input_tokens=1_000_000, cached_tokens=200_000, output_tokens=100_000),
        _row(
            task="python/beta",
            passed=False,
            input_tokens=1_000_000,
            cached_tokens=200_000,
            output_tokens=100_000,
            verdict="FailureClass.ERROR",
        ),
    ]
    run = _write_run(tmp_path, "priced", rows)
    pricing = _write_pricing(tmp_path / "pricing.toml")
    result = analyze_runs([run], out_dir=tmp_path / "out", pricing_path=pricing)
    overall = next(item for item in result.study.aggregates if item.category is None)
    assert overall.cost_per_attempt == 1.1
    assert overall.cost_per_success == 2.2
    assert result.study.currency == "USD"


def test_pricing_requires_cached_token_measurement(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "unknown-cache",
        [_row(cached_tokens=None), _row(task="python/beta", cached_tokens=None)],
    )
    pricing = _write_pricing(tmp_path / "pricing.toml")
    result = analyze_runs([run], out_dir=tmp_path / "out", pricing_path=pricing)
    overall = next(item for item in result.study.aggregates if item.category is None)
    assert overall.cost_per_attempt is None


def test_absent_pricing_is_unavailable_not_zero(tmp_path: Path) -> None:
    run = _write_run(tmp_path, "unpriced", [_row(), _row(task="python/beta")])
    result = analyze_runs([run], out_dir=tmp_path / "out")
    overall = next(item for item in result.study.aggregates if item.category is None)
    assert overall.total_cost is None
    assert overall.cost_per_attempt is None
    artifact = json.loads(result.recommendation_json.read_text(encoding="utf-8"))
    assert artifact["pricing"]["currency"] is None
    assert artifact["configurations"][0]["cost_per_attempt"] is None


def test_gate_breach_rate_and_failure_distribution(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "gates",
        [
            _row(),
            _row(
                task="python/beta",
                passed=False,
                verdict="FailureClass.ERROR",
                gate={"gate": "max_tokens", "observed": 101, "limit": 100},
            ),
        ],
    )
    result = analyze_runs([run], out_dir=tmp_path / "out")
    overall = next(item for item in result.study.aggregates if item.category is None)
    assert overall.gate_breach_rate == 0.5
    assert overall.failure_classes == {"token_budget_breach": 1}


def test_p95_small_samples_and_confidence_are_deterministic(tmp_path: Path) -> None:
    run = _write_run(tmp_path, "single", [_row()])
    observations, _ = load_observations([run], AnalysisConfig(), None)
    one = aggregate_group(observations, category=None, confidence=0.95)
    assert one.p95_latency_s is None
    assert wilson_interval(7, 10, 0.95) == wilson_interval(7, 10, 0.95)
    assert one.deployment_success_interval == (1.0, 1.0)
    assert one.iid_success_interval == wilson_interval(1, 1, 0.95)


def test_task_bootstrap_is_seeded_order_invariant_and_task_weighted(
    tmp_path: Path,
) -> None:
    rows = [
        *[_row(task="python/frequent", passed=True) for _ in range(10)],
        _row(task="python/rare", passed=False, verdict="FailureClass.ERROR"),
    ]
    run = _write_run(tmp_path, "clusters", rows)
    observations, _ = load_observations([run], AnalysisConfig(), None)
    first = aggregate_group(
        observations,
        category=None,
        confidence=0.95,
        statistics_seed=123,
    )
    reordered = aggregate_group(
        list(reversed(observations)),
        category=None,
        confidence=0.95,
        statistics_seed=123,
    )
    assert first.deployment_success_interval == reordered.deployment_success_interval
    assert first.bootstrap_seed == reordered.bootstrap_seed
    assert first.deployment_success_rate == 0.5
    assert first.success_rate == 10 / 11
    assert first.distinct_tasks == 2


def test_bootstrap_seed_can_change_nontrivial_interval() -> None:
    outcomes = {
        f"task/{index}": [True] * (((index * index + 3 * index + 1) % 7) + 1) + [False] * 9
        for index in range(4)
    }
    _, first = task_cluster_bootstrap(
        outcomes,
        confidence=0.873,
        seed=101,
    )
    _, second = task_cluster_bootstrap(
        outcomes,
        confidence=0.873,
        seed=202,
    )
    assert first != second


def test_scope_seed_is_stable_and_scope_specific() -> None:
    overall = stable_scope_seed(42, configuration_id="config-a", scope="overall")
    assert overall == stable_scope_seed(42, configuration_id="config-a", scope="overall")
    assert overall != stable_scope_seed(42, configuration_id="config-a", scope="bug-fix")
    assert overall != stable_scope_seed(43, configuration_id="config-a", scope="overall")


def test_overall_and_within_task_variance_are_distinct(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "variance",
        [
            _row(task="python/repeated", latency=10, input_tokens=100),
            _row(task="python/repeated", latency=10, input_tokens=100),
            _row(task="python/long", latency=100, input_tokens=10_000),
        ],
    )
    observations, _ = load_observations([run], AnalysisConfig(), None)
    aggregate = aggregate_group(observations, category=None, confidence=0.95)
    assert aggregate.overall_latency_variance is not None
    assert aggregate.overall_latency_variance > 0
    assert aggregate.median_within_task_latency_variance == 0.0
    assert aggregate.median_within_task_token_variance == 0.0
    assert aggregate.tasks_with_repeated_measurements == 1


def test_within_task_variance_unavailable_without_repetition(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "no-repeat",
        [_row(task="python/a"), _row(task="python/b")],
    )
    observations, _ = load_observations([run], AnalysisConfig(), None)
    aggregate = aggregate_group(observations, category=None, confidence=0.95)
    assert aggregate.median_within_task_latency_variance is None
    assert aggregate.tasks_with_repeated_measurements == 0


def test_pareto_frontier_marks_dominated_configuration(tmp_path: Path) -> None:
    rows = [
        _row(model="provider/model-a", task="python/alpha", latency=5),
        _row(model="provider/model-a", task="python/beta", latency=6),
        _row(
            model="provider/model-b",
            task="python/alpha",
            latency=20,
            passed=False,
            verdict="FailureClass.ERROR",
        ),
        _row(model="provider/model-b", task="python/beta", latency=22),
    ]
    run = _write_run(tmp_path, "pareto", rows)
    pricing = _write_pricing(tmp_path / "pricing.toml")
    observations, _ = load_observations(
        [run],
        AnalysisConfig(),
        # Exercise the public pricing loader through the service below.
        None,
    )
    assert len(observations) == 4
    result = analyze_runs([run], out_dir=tmp_path / "out", pricing_path=pricing)
    overall = [item for item in result.study.aggregates if item.category is None]
    frontier = pareto_analysis(overall, 0.0)
    by_model = {item.model: item for item in overall}
    assert by_model["provider/model-a"].configuration_id in frontier.reliability_cost
    assert by_model["provider/model-b"].configuration_id in frontier.dominated


def test_recommendation_abstention_constraints_and_tie_breaking(tmp_path: Path) -> None:
    run = _write_run(
        tmp_path,
        "recommend",
        [
            _row(model="provider/model-a"),
            _row(model="provider/model-b"),
        ],
    )
    observations, _ = load_observations([run], AnalysisConfig(), None)
    aggregates = build_aggregates(observations, AnalysisConfig())
    insufficient = recommend_scope(
        aggregates,
        scope="overall",
        policy=RecommendationConfig(minimum_sample_count=2),
    )
    assert insufficient.status == "insufficient_evidence"
    impossible = recommend_scope(
        aggregates,
        scope="overall",
        policy=RecommendationConfig(
            minimum_sample_count=1,
            maximum_cost_per_attempt=1.0,
        ),
    )
    assert impossible.status == "no_configuration_satisfies_constraints"
    first = recommend_scope(
        aggregates,
        scope="overall",
        policy=RecommendationConfig(minimum_sample_count=1),
    )
    second = recommend_scope(
        list(reversed(aggregates)),
        scope="overall",
        policy=RecommendationConfig(minimum_sample_count=1),
    )
    assert first.status == "recommended"
    assert first.primary is not None and second.primary is not None
    assert first.primary.configuration_id == second.primary.configuration_id


def test_outputs_are_versioned_self_contained_and_do_not_mutate_sources(
    tmp_path: Path,
) -> None:
    run = _write_run(tmp_path, "immutable", [_row(), _row(task="python/beta")])
    before = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}
    result = analyze_runs([run], out_dir=tmp_path / "analysis")
    after = {path.relative_to(run): path.read_bytes() for path in run.rglob("*") if path.is_file()}
    assert before == after
    artifact = json.loads(result.recommendation_json.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == 1
    assert result.markdown_report.is_file()
    html_report = result.html_report.read_text(encoding="utf-8")
    assert "<style>" in html_report
    assert "http://" not in html_report and "https://" not in html_report


def test_cli_analyze_writes_three_artifacts(tmp_path: Path) -> None:
    run = _write_run(tmp_path, "cli", [_row(), _row(task="python/beta")])
    out = tmp_path / "cli-out"
    result = CliRunner().invoke(app, ["analyze", str(run), "--out", str(out)])
    assert result.exit_code == 0, result.stdout
    assert (out / "recommendation.json").is_file()
    assert (out / "report.md").is_file()
    assert (out / "report.html").is_file()
