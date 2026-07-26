"""Versioned policy JSON and self-contained human report rendering."""

from __future__ import annotations

import html
import json
from dataclasses import asdict
from pathlib import Path

from danno_validator.analysis.models import (
    Aggregate,
    ParetoFrontier,
    Recommendation,
    Study,
)


def _aggregate_dict(aggregate: Aggregate) -> dict[str, object]:
    value = asdict(aggregate)
    value["deployment_success_interval"] = list(aggregate.deployment_success_interval)
    value["iid_success_interval"] = list(aggregate.iid_success_interval)
    value["task_consistency"] = [asdict(item) for item in aggregate.task_consistency]
    value["flip_tasks"] = list(aggregate.flip_tasks)
    value["warnings"] = list(aggregate.warnings)
    return value


def _selected(aggregate: Aggregate | None, reliability_basis: object) -> dict[str, object] | None:
    if aggregate is None:
        return None
    failures = sorted(aggregate.failure_classes.items(), key=lambda item: (-item[1], item[0]))
    return {
        "configuration_id": aggregate.configuration_id,
        "variant_id": aggregate.variant_id,
        "cohort_id": aggregate.cohort_id,
        "harness": aggregate.harness,
        "model": aggregate.model,
        "recommended_limits": None,
        "reliability_for_eligibility": {
            "basis": reliability_basis,
            "value": (
                aggregate.deployment_success_interval[0]
                if reliability_basis == "lower_confidence_bound"
                else aggregate.deployment_success_rate
            ),
        },
        "evidence": {
            "tasks": aggregate.distinct_tasks,
            "observations": aggregate.observations,
            "repetitions": aggregate.repetitions,
            "passed": aggregate.passed,
            "observed_success_rate": aggregate.success_rate,
            "deployment_success_rate": aggregate.deployment_success_rate,
            "deployment_success_interval": list(aggregate.deployment_success_interval),
            "interval_method": aggregate.success_interval_method,
            "task_clusters": aggregate.distinct_tasks,
            "evidence_quality": aggregate.evidence_quality,
        },
        "economics": {
            "cost_per_attempt": aggregate.cost_per_attempt,
            "cost_per_success": aggregate.cost_per_success,
        },
        "latency": {
            "average_s": aggregate.average_latency_s,
            "median_s": aggregate.median_latency_s,
            "p95_s": aggregate.p95_latency_s,
        },
        "dominant_failure_modes": [
            {"failure_class": failure_class, "count": count}
            for failure_class, count in failures[:3]
        ],
    }


def _recommendation_dict(recommendation: Recommendation) -> dict[str, object]:
    reliability_basis = recommendation.constraints["reliability_basis"]
    return {
        "status": recommendation.status,
        "scope": recommendation.scope,
        "objective": recommendation.objective,
        "constraints": recommendation.constraints,
        "primary": _selected(recommendation.primary, reliability_basis),
        "fallback": _selected(recommendation.fallback, reliability_basis),
        "reason": recommendation.reason,
        "warnings": list(recommendation.warnings),
    }


def policy_artifact(study: Study) -> dict[str, object]:
    by_scope = {item.scope: item for item in study.recommendations}
    return {
        "schema_version": 1,
        "study": study.name,
        "generated_at": study.generated_at,
        "source_runs": [str(path) for path in study.source_runs],
        "pricing": {
            "currency": study.currency,
            "effective_date": study.pricing_effective_date,
        },
        "methodology": study.methodology,
        "comparability_warnings": list(study.comparability_warnings),
        "overall": _recommendation_dict(by_scope["overall"]),
        "categories": {
            scope: _recommendation_dict(recommendation)
            for scope, recommendation in sorted(by_scope.items())
            if scope != "overall"
        },
        "configurations": [_aggregate_dict(item) for item in study.aggregates],
        "pareto": {
            "reliability_cost": _pareto_dict(study.pareto.reliability_cost),
            "reliability_latency": _pareto_dict(study.pareto.reliability_latency),
            "cost_latency": _pareto_dict(study.pareto.cost_latency),
        },
    }


def _pareto_dict(frontier: ParetoFrontier) -> dict[str, object]:
    return {
        "status": frontier.status,
        "configurations": list(frontier.configurations),
        "dominated": list(frontier.dominated),
        "reason": frontier.reason,
    }


def _fmt(value: int | float | None, *, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, int):
        return f"{value:,}{suffix}"
    return f"{value:,.{digits}f}{suffix}"


def _pct(value: float | None) -> str:
    return "unavailable" if value is None else f"{value * 100:.1f}%"


def _configuration_label(aggregate: Aggregate | None) -> str:
    return "none" if aggregate is None else f"`{aggregate.harness}` / `{aggregate.model}`"


def _recommendation_md(recommendation: Recommendation) -> list[str]:
    lines = [
        f"### {recommendation.scope}",
        "",
        f"- status: `{recommendation.status}`",
        f"- objective: `{recommendation.objective}`",
        f"- reliability eligibility: `{recommendation.constraints['reliability_basis']}`; "
        f"minimum {recommendation.constraints['minimum_success_rate']}",
        f"- evidence minimums: {recommendation.constraints['minimum_sample_count']} "
        f"observations and {recommendation.constraints['minimum_distinct_tasks']} distinct tasks",
        f"- primary: {_configuration_label(recommendation.primary)}",
        f"- fallback: {_configuration_label(recommendation.fallback)}",
        f"- rationale: {recommendation.reason}",
    ]
    if recommendation.primary is not None:
        primary = recommendation.primary
        lines += [
            f"- evidence: {primary.passed}/{primary.observations} passed across "
            f"{primary.distinct_tasks} task(s); {_pct(primary.success_rate)} "
            f"observed, with a {_pct(primary.deployment_success_interval[0])}–"
            f"{_pct(primary.deployment_success_interval[1])} task-bootstrap interval",
            f"- cost: {_fmt(primary.cost_per_attempt)} per attempt; "
            f"{_fmt(primary.cost_per_success)} per success",
            f"- latency: {_fmt(primary.average_latency_s, suffix='s')} average; "
            f"{_fmt(primary.p95_latency_s, suffix='s')} p95",
        ]
    return lines + [""]


def render_markdown(study: Study) -> str:
    overall = next(item for item in study.recommendations if item.scope == "overall")
    lines = [
        f"# {study.name}",
        "",
        "## Study overview",
        "",
        f"- generated: {study.generated_at}",
        f"- source runs: {len(study.source_runs)}",
        f"- observations: {len(study.observations)}",
        f"- configurations/cohorts: {len([a for a in study.aggregates if a.category is None])}",
        f"- confidence level: {study.confidence_level:.1%}",
        f"- pricing: {study.currency or 'not supplied'}"
        + (f" (effective {study.pricing_effective_date})" if study.pricing_effective_date else ""),
        "",
        "## Executive recommendation",
        "",
        *_recommendation_md(overall),
        "## Evidence quality and comparability",
        "",
    ]
    lines += (
        [f"- {warning}" for warning in study.comparability_warnings]
        if study.comparability_warnings
        else ["- No cross-cohort comparability warnings."]
    )
    lines += [
        "",
        "## Overall configuration comparison",
        "",
        "| configuration | cohort | n | tasks | passed | success (CI) | cost/attempt | "
        "cost/success | average latency | p95 latency | gate breaches | evidence |",
        "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    overall_aggregates = [item for item in study.aggregates if item.category is None]
    for item in overall_aggregates:
        lines.append(
            f"| `{item.harness}` / `{item.model}` | `{item.cohort_id}` | "
            f"{item.observations} | {item.distinct_tasks} | {item.passed} | "
            f"{_pct(item.success_rate)} ({_pct(item.deployment_success_interval[0])}–"
            f"{_pct(item.deployment_success_interval[1])}) | "
            f"{_fmt(item.cost_per_attempt)} | "
            f"{_fmt(item.cost_per_success)} | {_fmt(item.average_latency_s, suffix='s')} | "
            f"{_fmt(item.p95_latency_s, suffix='s')} | {_pct(item.gate_breach_rate)} | "
            f"{item.evidence_quality} |"
        )
    lookup = {item.configuration_id: item for item in overall_aggregates}

    def pareto_summary(frontier: ParetoFrontier) -> str:
        labels = ", ".join(
            f"`{lookup[item].harness}/{lookup[item].model}`"
            for item in frontier.configurations
            if item in lookup
        )
        detail = labels or "none"
        if frontier.reason:
            detail += f" — {frontier.reason}"
        return f"`{frontier.status}`: {detail}"

    lines += [
        "",
        "## Pareto-efficient configurations",
        "",
        f"- reliability versus cost: {pareto_summary(study.pareto.reliability_cost)}",
        f"- reliability versus p95 latency: {pareto_summary(study.pareto.reliability_latency)}",
        f"- cost versus p95 latency above the reliability threshold: "
        f"{pareto_summary(study.pareto.cost_latency)}",
        "",
        "## Results by task category",
        "",
    ]
    category_recommendations = [item for item in study.recommendations if item.scope != "overall"]
    if category_recommendations:
        for recommendation in category_recommendations:
            lines += _recommendation_md(recommendation)
    else:
        lines.append("No task categories were configured.")
        lines.append("")
    lines += [
        "## Reliability and repeated-run variance",
        "",
        "| configuration | repetitions | flip tasks | token variance | latency variance | "
        "cost variance |",
        "| --- | ---: | --- | ---: | ---: | ---: |",
    ]
    for item in overall_aggregates:
        lines.append(
            f"| `{item.harness}` / `{item.model}` | {item.repetitions} | "
            f"{', '.join(item.flip_tasks) or 'none'} | "
            f"{_fmt(item.overall_token_variance)} | "
            f"{_fmt(item.overall_latency_variance)} | "
            f"{_fmt(item.overall_cost_variance)} |"
        )
    lines += ["", "## Cost analysis", ""]
    if study.currency is None:
        lines.append(
            "No pricing file was supplied. Economic metrics are unavailable, never assumed zero."
        )
    else:
        lines.append(
            f"Costs use user-supplied {study.currency} rates effective "
            f"{study.pricing_effective_date}; cached input is priced separately when measured."
        )
    lines += [
        "",
        "## Latency and resource analysis",
        "",
        "| configuration | average latency | median latency | p95 latency | peak CPU | "
        "peak RAM (KB) | peak GPU | peak VRAM (MB) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in overall_aggregates:
        lines.append(
            f"| `{item.harness}` / `{item.model}` | "
            f"{_fmt(item.average_latency_s, suffix='s')} | "
            f"{_fmt(item.median_latency_s, suffix='s')} | "
            f"{_fmt(item.p95_latency_s, suffix='s')} | "
            f"{_fmt(item.peak_cpu_pct, suffix='%')} | {_fmt(item.peak_ram_kb)} | "
            f"{_fmt(item.peak_gpu_pct, suffix='%')} | {_fmt(item.peak_vram_mb)} |"
        )
    lines += [
        "",
        "Resource aggregates require complete row coverage and are kept in their native "
        "units rather than collapsed into a composite score.",
        "",
        "## Failure-class distribution",
        "",
    ]
    for item in overall_aggregates:
        failures = ", ".join(
            f"`{failure}` {count}" for failure, count in item.failure_classes.items()
        )
        lines.append(f"- `{item.harness}` / `{item.model}`: {failures or 'no failures'}")
    lines += [
        "",
        "## Task-level results",
        "",
        "| run | configuration | task | category | attempt | result | failure | tokens | "
        "latency | cost |",
        "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: |",
    ]
    for row in study.observations:
        lines.append(
            f"| `{row.run_id}` | `{row.harness}` / `{row.model}` | "
            f"`{row.suite}/{row.task}` | {row.category or 'unmapped'} | "
            f"`{row.attempt_id}` | {'pass' if row.passed else 'fail'} | "
            f"`{row.failure_class}` | {_fmt(row.total_tokens)} | "
            f"{_fmt(row.latency_s, suffix='s')} | {_fmt(row.estimated_cost)} |"
        )
    lines += [
        "",
        "## Provenance and methodology",
        "",
        "- Observations are post-processed from existing artifacts; source directories are "
        "never modified.",
        "- Success uncertainty uses a Wilson score interval. P95 uses nearest-rank and is "
        "unavailable for fewer than two observations.",
        "- Repeated trials are grouped by configuration cohort and suite/task. Flip tasks have "
        "both passing and failing outcomes.",
        "- Recommendations first enforce hard constraints, then apply the configured objective "
        "and deterministic tie-breaking recorded in the policy artifact.",
        "- No independence claim is made for repeated executions, and pass@k is deliberately "
        "not reported because current artifacts do not establish independent sampling.",
        "",
    ]
    return "\n".join(lines)


_CSS = """
:root{--ink:#172026;--muted:#60707a;--line:#d9e1e5;--paper:#fff;--bg:#f3f6f7;
--accent:#087f8c;--good:#197044;--warn:#915c00}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1180px;margin:auto;padding:36px 24px 72px}h1{font-size:30px;margin:0 0 20px}
h2{font-size:17px;margin-top:34px;border-bottom:1px solid var(--line);padding-bottom:7px}
h3{font-size:15px}.card{background:var(--paper);border:1px solid var(--line);border-radius:10px;
padding:16px 20px;margin:12px 0}.meta{color:var(--muted)}
.status{font-weight:700;color:var(--accent)}
.table{overflow:auto;background:var(--paper);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:8px 10px;
border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}th{color:var(--muted);
font-size:11px;text-transform:uppercase}code{font-size:12px}li{margin:4px 0}
"""


def render_html(study: Study) -> str:
    overall = next(item for item in study.recommendations if item.scope == "overall")
    primary = _configuration_label(overall.primary).replace("`", "")
    overall_aggregates = [item for item in study.aggregates if item.category is None]
    comparison_rows = "".join(
        "<tr>"
        f"<td>{html.escape(item.harness)}</td><td>{html.escape(item.model)}</td>"
        f"<td>{item.observations}</td><td>{item.distinct_tasks}</td>"
        f"<td>{html.escape(_pct(item.success_rate))}</td>"
        f"<td>{html.escape(_pct(item.deployment_success_interval[0]))}–"
        f"{html.escape(_pct(item.deployment_success_interval[1]))}</td>"
        f"<td>{html.escape(_fmt(item.cost_per_attempt))}</td>"
        f"<td>{html.escape(_fmt(item.p95_latency_s, suffix='s'))}</td>"
        f"<td>{html.escape(item.evidence_quality)}</td></tr>"
        for item in overall_aggregates
    )
    warnings = "".join(
        f"<li>{html.escape(warning)}</li>" for warning in study.comparability_warnings
    )
    categories = "".join(
        f'<div class="card"><h3>{html.escape(item.scope)}</h3>'
        f'<p class="status">{html.escape(item.status)}</p>'
        f"<p>{html.escape(item.reason)}</p></div>"
        for item in study.recommendations
        if item.scope != "overall"
    )
    lookup = {item.configuration_id: item for item in overall_aggregates}

    def labels(frontier: ParetoFrontier) -> str:
        ids = frontier.configurations
        return (
            ", ".join(
                f"{lookup[item].harness}/{lookup[item].model}" for item in ids if item in lookup
            )
            or frontier.status
        )

    reliability_rows = "".join(
        "<tr>"
        f"<td>{html.escape(item.harness)}/{html.escape(item.model)}</td>"
        f"<td>{item.repetitions}</td>"
        f"<td>{html.escape(', '.join(item.flip_tasks) or 'none')}</td>"
        f"<td>{html.escape(_fmt(item.overall_token_variance))}</td>"
        f"<td>{html.escape(_fmt(item.overall_latency_variance))}</td>"
        f"<td>{html.escape(_fmt(item.overall_cost_variance))}</td></tr>"
        for item in overall_aggregates
    )
    resource_rows = "".join(
        "<tr>"
        f"<td>{html.escape(item.harness)}/{html.escape(item.model)}</td>"
        f"<td>{html.escape(_fmt(item.average_latency_s, suffix='s'))}</td>"
        f"<td>{html.escape(_fmt(item.p95_latency_s, suffix='s'))}</td>"
        f"<td>{html.escape(_fmt(item.peak_cpu_pct, suffix='%'))}</td>"
        f"<td>{html.escape(_fmt(item.peak_ram_kb))}</td>"
        f"<td>{html.escape(_fmt(item.peak_gpu_pct, suffix='%'))}</td>"
        f"<td>{html.escape(_fmt(item.peak_vram_mb))}</td></tr>"
        for item in overall_aggregates
    )
    failures = "".join(
        f"<li><strong>{html.escape(item.harness)}/{html.escape(item.model)}</strong>: "
        + html.escape(
            ", ".join(f"{name} {count}" for name, count in item.failure_classes.items())
            or "no failures"
        )
        + "</li>"
        for item in overall_aggregates
    )
    task_rows = "".join(
        "<tr>"
        f"<td>{html.escape(row.harness)}/{html.escape(row.model)}</td>"
        f"<td>{html.escape(row.suite)}/{html.escape(row.task)}</td>"
        f"<td>{html.escape(row.category or 'unmapped')}</td>"
        f"<td>{'pass' if row.passed else 'fail'}</td>"
        f"<td>{html.escape(row.failure_class)}</td>"
        f"<td>{html.escape(_fmt(row.total_tokens))}</td>"
        f"<td>{html.escape(_fmt(row.latency_s, suffix='s'))}</td>"
        f"<td>{html.escape(_fmt(row.estimated_cost))}</td></tr>"
        for row in study.observations
    )
    cost_note = (
        f"<p>Uses user-supplied {html.escape(study.currency)} pricing effective "
        f"{html.escape(study.pricing_effective_date or 'unknown')}.</p>"
        if study.currency
        else "<p>No pricing supplied; costs are unavailable, not zero.</p>"
    )
    methodology = json.dumps(study.methodology, sort_keys=True)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(study.name)}</title><style>{_CSS}</style></head><body><main>"
        f"<h1>{html.escape(study.name)}</h1>"
        f'<p class="meta">Generated {html.escape(study.generated_at)} · '
        f"{len(study.observations)} observations · confidence {study.confidence_level:.1%}</p>"
        '<section class="card"><h2>Executive recommendation</h2>'
        f'<p class="status">{html.escape(overall.status)}</p>'
        f"<p><strong>{html.escape(primary)}</strong></p>"
        f"<p>{html.escape(overall.reason)}</p></section>"
        "<h2>Evidence quality and comparability</h2>"
        f"<ul>{warnings or '<li>No cross-cohort warnings.</li>'}</ul>"
        '<h2>Overall configuration comparison</h2><div class="table"><table><thead><tr>'
        "<th>Harness</th><th>Model</th><th>n</th><th>Tasks</th><th>Success</th>"
        "<th>Confidence interval</th><th>Cost/attempt</th><th>P95 latency</th>"
        f"<th>Evidence</th></tr></thead><tbody>{comparison_rows}</tbody></table></div>"
        "<h2>Pareto-efficient configurations</h2><ul>"
        f"<li>Reliability versus cost: {html.escape(labels(study.pareto.reliability_cost))}</li>"
        f"<li>Reliability versus latency: "
        f"{html.escape(labels(study.pareto.reliability_latency))}</li>"
        f"<li>Cost versus latency above threshold: "
        f"{html.escape(labels(study.pareto.cost_latency))}</li></ul>"
        "<h2>Results by task category</h2>"
        f"{categories or '<p>No categories configured.</p>'}"
        "<h2>Reliability and repeated-run variance</h2>"
        '<div class="table"><table><thead><tr><th>Configuration</th><th>Repetitions</th>'
        "<th>Flip tasks</th><th>Token variance</th><th>Latency variance</th>"
        f"<th>Cost variance</th></tr></thead><tbody>{reliability_rows}</tbody></table></div>"
        "<h2>Cost analysis</h2>"
        f"{cost_note}"
        "<h2>Latency and resource analysis</h2>"
        '<div class="table"><table><thead><tr><th>Configuration</th><th>Average latency</th>'
        "<th>P95 latency</th><th>Peak CPU</th><th>Peak RAM KB</th><th>Peak GPU</th>"
        f"<th>Peak VRAM MB</th></tr></thead><tbody>{resource_rows}</tbody></table></div>"
        "<h2>Failure-class distribution</h2>"
        f"<ul>{failures}</ul>"
        "<h2>Task-level results</h2>"
        '<div class="table"><table><thead><tr><th>Configuration</th><th>Task</th>'
        "<th>Category</th><th>Result</th><th>Failure</th><th>Tokens</th>"
        f"<th>Latency</th><th>Cost</th></tr></thead><tbody>{task_rows}</tbody></table></div>"
        "<h2>Provenance and methodology</h2>"
        "<p>Post-processing only. Missing values remain unavailable. Wilson confidence "
        "intervals and deterministic constraint-aware ranking are used; no repeated-run "
        "independence is assumed.</p>"
        f'<p class="meta">Machine-readable methodology: {html.escape(methodology)}</p>'
        "</main></body></html>"
    )


def write_outputs(study: Study, out_dir: Path) -> tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "recommendation.json"
    markdown_path = out_dir / "report.md"
    html_path = out_dir / "report.html"
    json_path.write_text(
        json.dumps(policy_artifact(study), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(study), encoding="utf-8")
    html_path.write_text(render_html(study), encoding="utf-8")
    return json_path, markdown_path, html_path
