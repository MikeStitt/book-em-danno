"""Render Level-0 results as MyST-Markdown pages.

M1 emitted one page per config; M2 adds the **sweep index**: a results matrix over
many configs plus a `{toctree}` linking each per-config page. Both are rendered
with stdlib string building (no Jinja2/Sphinx dependency yet) — a handful of rows
and a toctree need no template engine, so the `danno[validator]` extra stays empty
until the judge (M6) brings the Anthropic SDK. MyST is just Markdown here, so the
pages also read fine raw.

Transcripts are sanitised before they reach the page: ANSI escapes stripped, and
raw model output fenced so stray backticks or markup can't break the document.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from danno_validator.baseline import BASELINE_MODEL
from danno_validator.level0 import ConversationResult, TurnRecord
from danno_validator.level1 import TaskResult
from danno_validator.level2 import DevTaskResult
from danno_validator.oracle import FailureClass
from danno_validator.sweep import SweepResult

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# A glanceable mark per verdict for the page heading and (later) the matrix.
_BADGE = {
    FailureClass.PASS: "✓ pass",
    FailureClass.STALL: "✗ stall (promised-but-didn't-act)",
    FailureClass.ONLY_ACTS_ON_NUDGE: "~ only-acts-on-nudge",
    FailureClass.HALLUCINATED_TOOL: "✗ hallucinated-tool",
    FailureClass.REFUSAL: "✗ refusal",
    FailureClass.EARLY_STOP: "✗ early-stop",
    FailureClass.MALFORMED_TOOL_ARGS: "✗ malformed-tool-args",
    FailureClass.LOOP: "✗ loop",
    FailureClass.ERROR: "! error",
}


def verdict_label(cls: FailureClass) -> str:
    """The glanceable badge for a verdict (e.g. `✓ pass`, `✗ early-stop`).

    Shared by the report and the `danno validate` live status / summary so the two
    surfaces always show the same wording for a given `FailureClass`.
    """
    return _BADGE.get(cls, cls.value)


def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences so they don't leak into the report."""
    return _ANSI_RE.sub("", text)


def slug(model: str) -> str:
    """A filesystem/anchor-safe slug for a model id (e.g. ollama/gemma3:27b)."""
    return _SLUG_RE.sub("-", model.lower()).strip("-")


def _fence(text: str, *, lang: str = "") -> str:
    """Fence text safely even if it contains triple backticks (use a longer fence)."""
    body = strip_ansi(text).rstrip()
    fence = "```"
    while fence in body:
        fence += "`"
    return f"{fence}{lang}\n{body}\n{fence}"


def _turn_section(record: TurnRecord) -> str:
    v = record.verdict
    reply = record.turn.assistant_text or "(no assistant text)"
    lines = [
        f"### Turn: {record.label}",
        "",
        "**Prompt sent**",
        "",
        _fence(record.prompt),
        "",
        "**Assistant reply**",
        "",
        _fence(reply),
        "",
        f"- verdict: `{v.failure_class.value}` — {v.rationale}",
        f"- tool calls: {v.tool_call_count}"
        + (
            " (" + ", ".join(f"`{c.get('tool')}`" for c in record.turn.tool_calls) + ")"
            if record.turn.tool_calls
            else ""
        ),
        f"- promised action: {'yes' if v.promised_action else 'no'}"
        f" · workspace side effect: {'yes' if v.side_effect else 'no'}",
        f"- latency: {record.latency_s:.1f}s · tokens: {record.turn.tokens}",
    ]
    if record.turn.errors:
        lines.append(f"- error: {record.turn.error_summary}")
    return "\n".join(lines)


def _level1_section(tr: TaskResult) -> str:
    """Render the Level-1 tool/bash result as a section appended to a config page."""
    v = tr.verdict
    badge = _BADGE.get(v.failure_class, v.failure_class.value)
    reply = tr.turn.assistant_text or "(no assistant text)"
    tools = (
        " (" + ", ".join(f"`{c.get('tool')}`" for c in tr.turn.tool_calls) + ")"
        if tr.turn.tool_calls
        else ""
    )
    lines = [
        "## Level 1 — tool/bash",
        "",
        f"**Verdict: {badge}** · task `{tr.task_label}`",
        "",
        "**Assistant reply**",
        "",
        _fence(reply),
        "",
        f"- verdict: `{v.failure_class.value}` — {v.rationale}",
        f"- tool calls: {v.tool_call_count}{tools}",
        f"- workspace side effect: {'yes' if v.side_effect else 'no'}",
        f"- latency: {tr.latency_s:.1f}s · tokens: {tr.turn.tokens}",
    ]
    if tr.turn.errors:
        lines.append(f"- error: {tr.turn.error_summary}")
    return "\n".join(lines)


def _level2_section(dr: DevTaskResult) -> str:
    """Render the Level-2 dev result (hidden test suite) as an appended section."""
    v = dr.verdict
    badge = _BADGE.get(v.failure_class, v.failure_class.value)
    reply = dr.turn.assistant_text or "(no assistant text)"
    tools = (
        " (" + ", ".join(f"`{c.get('tool')}`" for c in dr.turn.tool_calls) + ")"
        if dr.turn.tool_calls
        else ""
    )
    test_output = (dr.test_run.stdout + dr.test_run.stderr).strip() or "(no test output)"
    lines = [
        "## Level 2 — software dev",
        "",
        f"**Verdict: {badge}** · task `{dr.task_label}`",
        "",
        "**Assistant reply**",
        "",
        _fence(reply),
        "",
        f"- verdict: `{v.failure_class.value}` — {v.rationale}",
        f"- tool calls: {v.tool_call_count}{tools}",
        f"- hidden tests: {'passed' if dr.test_run.passed else 'failed'} "
        f"(`{dr.test_run.command}`, exit {dr.test_run.returncode})",
        f"- latency: {dr.latency_s:.1f}s · tokens: {dr.turn.tokens}",
        "",
        "**Hidden test output**",
        "",
        _fence(test_output),
    ]
    if dr.turn.errors:
        lines.append(f"- error: {dr.turn.error_summary}")
    return "\n".join(lines)


def render_level0_page(
    result: ConversationResult,
    *,
    opencode_jsonc_excerpt: str | None = None,
    level1: TaskResult | None = None,
    level2: DevTaskResult | None = None,
) -> str:
    """Render one config's report as a MyST-Markdown document.

    The Level-0 transcript is always rendered; when `level1`/`level2` are supplied
    (the config reached that tier) their sections are appended in tier order.
    """
    badge = _BADGE.get(result.overall, result.overall.value)
    parts = [
        f"# Level 0 — `{result.model}`",
        "",
        f"**Verdict: {badge}**",
        "",
        "| field | value |",
        "| --- | --- |",
        f"| model | `{result.model}` |",
        f"| sandbox | `{result.sandbox}` |",
        f"| workspace | `{result.workspace_root}` |",
        f"| session | `{result.session_id or 'n/a'}` |",
        f"| turns | {len(result.records)} |",
        f"| total tokens | {result.total_tokens} |",
        f"| total cost | {result.total_cost:.4f} |",
        f"| total latency | {result.total_latency_s:.1f}s |",
        "",
    ]
    if opencode_jsonc_excerpt:
        excerpt = _fence(opencode_jsonc_excerpt, lang="json")
        parts += ["## opencode.jsonc (excerpt)", "", excerpt, ""]
    parts += ["## Level 0 — liveness", ""]
    parts += [_turn_section(r) + "\n" for r in result.records]
    if level1 is not None:
        parts += [_level1_section(level1), ""]
    if level2 is not None:
        parts += [_level2_section(level2), ""]
    return "\n".join(parts).rstrip() + "\n"


def write_level0_page(
    result: ConversationResult,
    out_dir: Path,
    *,
    opencode_jsonc_excerpt: str | None = None,
    level1: TaskResult | None = None,
    level2: DevTaskResult | None = None,
) -> Path:
    """Render and write one config's page to `out_dir/level0-<model-slug>.md`.

    Returns the written path. When `level1`/`level2` are supplied they are appended
    as the tool/bash and software-dev sections. `out_dir` is created if missing
    (typically a subdirectory of `driver.DEFAULT_WORK_DIR`, which is gitignored).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"level0-{slug(result.model)}.md"
    path.write_text(
        render_level0_page(
            result, opencode_jsonc_excerpt=opencode_jsonc_excerpt, level1=level1, level2=level2
        )
    )
    return path


def _toctree(doc_stems: list[str]) -> str:
    """A MyST `{toctree}` directive linking the per-config pages by filename stem."""
    entries = "\n".join(doc_stems)
    return f"```{{toctree}}\n:maxdepth: 1\n\n{entries}\n```"


def render_matrix_index(
    results: list[SweepResult],
    doc_stems: list[str],
    *,
    title: str = "danno-validator — tiered sweep (L0 + L1 + L2)",
) -> str:
    """Render the sweep index: a results matrix, a failure-taxonomy summary, and a
    toctree of the per-config pages.

    Rows are the swept configs (one per model variant) in sweep order, plus the
    Claude Code baseline row (flagged) when present; `doc_stems` are the per-config
    page filenames (without extension) the toctree links — kept as an explicit
    argument so the index always matches what `write_sweep_report` actually wrote.
    The baseline is the reference point, so it is excluded from the swept-config
    tally and the failure-taxonomy counts (which describe the models under test).
    """
    configs = [s for s in results if s.variant.model_name != BASELINE_MODEL]
    has_baseline = any(s.variant.model_name == BASELINE_MODEL for s in results)
    passed = sum(1 for s in configs if s.result.passed)
    summary = f"{len(configs)} config(s) swept · {passed} passed · {len(configs) - passed} failed."
    if has_baseline:
        summary += " · + Claude Code baseline (reference row)."
    parts = [
        f"# {title}",
        "",
        summary,
        "",
        "## Results matrix",
        "",
        "| config | model | L0 verdict | L1 verdict | L2 verdict | turns | tokens | latency |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in results:
        r = s.result
        badge = _BADGE.get(r.overall, r.overall.value)
        # "—" reads as "not run": each higher tier is skipped whenever the previous
        # tier didn't pass (the L0→L1→L2 short-circuit).
        l1_badge = (
            _BADGE.get(s.level1.overall, s.level1.overall.value) if s.level1 is not None else "—"
        )
        l2_badge = (
            _BADGE.get(s.level2.overall, s.level2.overall.value) if s.level2 is not None else "—"
        )
        # Flag the baseline so it reads as the reference rather than a swept config.
        config_cell = f"`{s.variant.model_name}`"
        if s.variant.model_name == BASELINE_MODEL:
            config_cell += " _(baseline)_"
        parts.append(
            f"| {config_cell} | `{s.variant.model_ref}` | {badge} | {l1_badge} "
            f"| {l2_badge} | {len(r.records)} | {r.total_tokens} | {r.total_latency_s:.1f}s |"
        )
    parts += ["", "## Failure taxonomy", ""]
    counts = Counter(s.result.overall for s in configs)
    for cls, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].value)):
        parts.append(f"- `{cls.value}`: {n}")
    parts += ["", "## Per-config reports", "", _toctree(doc_stems), ""]
    return "\n".join(parts).rstrip() + "\n"


def write_sweep_report(results: list[SweepResult], out_dir: Path) -> tuple[list[Path], Path]:
    """Write one Level-0 page per swept config plus an `index.md` matrix into
    `out_dir`. Returns `(per_config_paths, index_path)`.

    The toctree in the index is built from the actual written filenames, so the
    index and its linked pages can never drift out of sync. Stale `level0-*.md`
    pages from a prior run are pruned first, so a re-run with a different model
    set (different page slugs) leaves no orphaned pages the index no longer links
    — only the per-config pages this writer owns are removed; anything else in
    `out_dir` is left untouched.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("level0-*.md"):
        stale.unlink()
    page_paths = [
        write_level0_page(s.result, out_dir, level1=s.level1, level2=s.level2) for s in results
    ]
    index = render_matrix_index(results, [p.stem for p in page_paths])
    index_path = out_dir / "index.md"
    index_path.write_text(index)
    return page_paths, index_path
