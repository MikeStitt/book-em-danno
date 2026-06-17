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

from danno_validator.level0 import ConversationResult, TurnRecord
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


def render_level0_page(
    result: ConversationResult, *, opencode_jsonc_excerpt: str | None = None
) -> str:
    """Render the Level-0 result for one config as a MyST-Markdown document."""
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
    parts += ["## Transcript", ""]
    parts += [_turn_section(r) + "\n" for r in result.records]
    return "\n".join(parts).rstrip() + "\n"


def write_level0_page(
    result: ConversationResult,
    out_dir: Path,
    *,
    opencode_jsonc_excerpt: str | None = None,
) -> Path:
    """Render and write the Level-0 page to `out_dir/level0-<model-slug>.md`.

    Returns the written path. `out_dir` is created if missing (typically a
    subdirectory of `driver.DEFAULT_WORK_DIR`, which is gitignored).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"level0-{slug(result.model)}.md"
    path.write_text(render_level0_page(result, opencode_jsonc_excerpt=opencode_jsonc_excerpt))
    return path


def _toctree(doc_stems: list[str]) -> str:
    """A MyST `{toctree}` directive linking the per-config pages by filename stem."""
    entries = "\n".join(doc_stems)
    return f"```{{toctree}}\n:maxdepth: 1\n\n{entries}\n```"


def render_matrix_index(
    results: list[SweepResult],
    doc_stems: list[str],
    *,
    title: str = "danno-validator — Level 0 sweep",
) -> str:
    """Render the sweep index: a results matrix, a failure-taxonomy summary, and a
    toctree of the per-config pages.

    Rows are the swept configs (one per model variant) in sweep order; `doc_stems`
    are the per-config page filenames (without extension) the toctree links — kept
    as an explicit argument so the index always matches what `write_sweep_report`
    actually wrote.
    """
    passed = sum(1 for s in results if s.result.passed)
    parts = [
        f"# {title}",
        "",
        f"{len(results)} config(s) swept · {passed} passed · {len(results) - passed} failed.",
        "",
        "## Results matrix",
        "",
        "| config | model | L0 verdict | turns | tokens | latency |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for s in results:
        r = s.result
        badge = _BADGE.get(r.overall, r.overall.value)
        parts.append(
            f"| `{s.variant.model_name}` | `{s.variant.model_ref}` | {badge} "
            f"| {len(r.records)} | {r.total_tokens} | {r.total_latency_s:.1f}s |"
        )
    parts += ["", "## Failure taxonomy", ""]
    counts = Counter(s.result.overall for s in results)
    for cls, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].value)):
        parts.append(f"- `{cls.value}`: {n}")
    parts += ["", "## Per-config reports", "", _toctree(doc_stems), ""]
    return "\n".join(parts).rstrip() + "\n"


def write_sweep_report(results: list[SweepResult], out_dir: Path) -> tuple[list[Path], Path]:
    """Write one Level-0 page per swept config plus an `index.md` matrix into
    `out_dir`. Returns `(per_config_paths, index_path)`.

    The toctree in the index is built from the actual written filenames, so the
    index and its linked pages can never drift out of sync.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    page_paths = [write_level0_page(s.result, out_dir) for s in results]
    index = render_matrix_index(results, [p.stem for p in page_paths])
    index_path = out_dir / "index.md"
    index_path.write_text(index)
    return page_paths, index_path
