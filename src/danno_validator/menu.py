"""Emit the annotated "menu" danno.toml — the validator's signature deliverable.

The sweep (`sweep.run_sweep`) tells us which declared models actually clear the
tiered battery; this module turns that knowledge into a danno.toml the user can
adopt. It round-trips the *whole* source config (so nothing — backends, tools,
npm plugins — is silently dropped, Working Rule 8) and layers the validation
verdict onto the model-selection surface:

* every `[models.*]` block is preceded by a `# [L0 … · L1 … · L2 …]` verdict
  comment derived from that model's `SweepResult`; and
* `[agents]` becomes a *menu* — each role keeps its active assignment (annotated
  with that model's verdict) followed by every other model as a commented-out
  alternative, so the user picks an assignment by uncommenting (TOML holds one
  value per key, so the choice is comment/uncomment, not two live values).

The Claude Code baseline row (`baseline.BASELINE_MODEL`) is the reference point,
not a danno.toml-declarable model, so it is excluded from the menu.

TOML is emitted by hand (a small generic value serializer) rather than via a
writer library: the deliverable is fundamentally a *commented* document, which no
serializer produces, and the repo carries no TOML-writer dependency. The approach
mirrors `report.py`'s stdlib string building.
"""

from __future__ import annotations

from pathlib import Path

from book_em_danno.config.generate import agent_model_name
from book_em_danno.config.schema import DannoConfig
from danno_validator.baseline import BASELINE_MODEL
from danno_validator.oracle import FailureClass
from danno_validator.sweep import SweepResult

# Glanceable single-char marks per verdict, used inside the compact `[L0 · L1 · L2]`
# badge (the report uses longer badges; the menu needs them to fit in a comment).
_MARK = {
    FailureClass.PASS: "✓",
    FailureClass.ONLY_ACTS_ON_NUDGE: "~",
    FailureClass.ERROR: "!",
}
_FAIL_MARK = "✗"
_NOT_RUN = "–"  # tier skipped by the short-circuit (an earlier tier didn't pass)


def _tier_badge(label: str, overall: FailureClass | None) -> str:
    """One tier's piece of the verdict badge, e.g. `L2 ✗ early-stop` or `L1 –`.

    `overall` is `None` when the tier was skipped (the L0→L1→L2 short-circuit);
    a passing tier shows just its mark, a failing one appends the class so the
    reason is legible at a glance.
    """
    if overall is None:
        return f"{label} {_NOT_RUN}"
    mark = _MARK.get(overall, _FAIL_MARK)
    if overall is FailureClass.PASS:
        return f"{label} {mark}"
    return f"{label} {mark} {overall.value}"


def verdict_badge(result: SweepResult) -> str:
    """The compact `[L0 … · L1 … · L2 …]` verdict for one swept config.

    Reads the tiered outcome the same way the report does: `result.result` is the
    Level-0 verdict; `level1`/`level2` are `None` when that tier was short-circuited.
    """
    l1 = result.level1.overall if result.level1 is not None else None
    l2 = result.level2.overall if result.level2 is not None else None
    parts = [
        _tier_badge("L0", result.result.overall),
        _tier_badge("L1", l1),
        _tier_badge("L2", l2),
    ]
    return "[" + " · ".join(parts) + "]"


def is_recommended(result: SweepResult) -> bool:
    """True when a config cleared all three tiers — flagged RECOMMENDED in the menu."""
    return (
        result.result.passed
        and result.level1 is not None
        and result.level1.passed
        and result.level2 is not None
        and result.level2.passed
    )


def _model_comment(result: SweepResult | None, *, verified: str | None) -> str:
    """The `# [...]` verdict comment line that precedes a `[models.*]` block."""
    if result is None:
        return "# [not validated — outside the swept set]"
    badge = verdict_badge(result)
    suffix = "  RECOMMENDED" if is_recommended(result) else ""
    stamp = f"  — verified {verified}" if verified else ""
    return f"# {badge}{suffix}{stamp}"


def _fmt_str(value: str) -> str:
    """A double-quoted TOML basic string with the metacharacters escaped."""
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _fmt_value(value: object) -> str:
    """Serialize a scalar / list / inline-table value to TOML text. Fails loud on
    an unsupported type rather than emitting something that won't parse."""
    # bool first: it is a subclass of int.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _fmt_str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_fmt_value(v) for v in value) + "]"
    if isinstance(value, dict):
        body = ", ".join(f"{k} = {_fmt_value(v)}" for k, v in value.items())
        return "{ " + body + " }"
    raise TypeError(f"cannot serialize {type(value).__name__} to TOML: {value!r}")


def _kv_lines(data: dict[str, object]) -> list[str]:
    """`key = value` lines for a table, skipping empty/None fields so the menu
    shows only what the source config actually set."""
    lines = []
    for key, value in data.items():
        if value is None or value == [] or value == {}:
            continue
        lines.append(f"{key} = {_fmt_value(value)}")
    return lines


def _table(header: str, data: dict[str, object]) -> list[str]:
    """A `[header]` block followed by its key/value lines, then a blank line."""
    return [header, *_kv_lines(data), ""]


def _agents_menu(config: DannoConfig, by_model: dict[str, SweepResult]) -> list[str]:
    """Render `[agents]` as a comment/uncomment menu.

    For each assigned role the active line carries that model's verdict badge,
    followed by every *other* declared model as a commented-out alternative with
    its own badge — so the user swaps a role's model by uncommenting one line. The
    menu is a model-selection surface: a role's selected model is read from either
    [agents] form (the string shorthand or the rich form's `model`).
    """

    def badge_comment(model_name: str | None) -> str:
        result = by_model.get(model_name) if model_name is not None else None
        return verdict_badge(result) if result is not None else "[not validated]"

    lines = ["[agents]"]
    if not config.agents:
        lines.append("# (no agent assignments in the source danno.toml)")
        lines.append("")
        return lines
    all_models = sorted(config.models)
    for role, assigned in config.agents.items():
        selected = agent_model_name(assigned) or ""
        lines.append(f'{role} = "{selected}"   # {badge_comment(selected or None)}')
        for alt in all_models:
            if alt != selected:
                lines.append(f'# {role} = "{alt}"   # {badge_comment(alt)} — uncomment to use')
    lines.append("")
    return lines


_HEADER = (
    '# danno.toml — annotated "menu" generated by danno-validator.\n'
    "#\n"
    "# Every [models.*] block carries its tiered validation verdict as a preceding\n"
    "# comment: [L0 · L1 · L2], where ✓ = pass, ✗ = fail, ~ = only-acts-on-nudge,\n"
    "# ! = error, – = not run (an earlier tier short-circuited). Under [agents] each\n"
    "# role's active assignment is followed by commented alternatives — uncomment one\n"
    "# to swap which model serves that role. Verdicts are comments only (TOML holds\n"
    "# one value per key); you assemble a working config by editing the assignments."
)


def render_menu(
    config: DannoConfig,
    results: list[SweepResult],
    *,
    verified: str | None = None,
) -> str:
    """Render the annotated menu danno.toml as text.

    `results` is the sweep outcome; the Claude Code baseline row is excluded (it is
    a reference, not a declarable model). `verified` (e.g. `"2026-06-18"`), when
    given, is stamped onto each model's verdict comment. Models the sweep didn't
    cover (outside an `only` subset) are emitted with a "not validated" comment so
    the round-trip stays complete.
    """
    by_model = {s.variant.model_name: s for s in results if s.variant.model_name != BASELINE_MODEL}
    parts = [_HEADER, ""]
    parts += _table("[project]", config.project.model_dump())
    parts += _table("[defaults]", config.defaults.model_dump())
    parts += _table("[sandbox]", config.sandbox.model_dump())
    for name in sorted(config.backends):
        parts += _table(f"[backends.{name}]", config.backends[name].model_dump(exclude_none=True))
    for name in sorted(config.models):
        parts.append(_model_comment(by_model.get(name), verified=verified))
        parts += _table(f"[models.{name}]", config.models[name].model_dump(exclude_none=True))
    parts += _agents_menu(config, by_model)
    for tool in config.tools:
        parts += _table("[[tools]]", tool.model_dump(exclude_none=True))
    for plugin in config.npm:
        parts += _table("[[npm]]", plugin.model_dump(exclude_none=True))
    return "\n".join(parts).rstrip() + "\n"


def write_menu(
    config: DannoConfig,
    results: list[SweepResult],
    out_path: Path,
    *,
    verified: str | None = None,
) -> Path:
    """Render and write the menu danno.toml to `out_path`. Returns the path.

    The parent directory is created if missing. The written file is itself a valid,
    loadable danno.toml (verdict annotations are comments), so the user can edit the
    agent assignments and feed it straight back to `danno`.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_menu(config, results, verified=verified))
    return out_path
