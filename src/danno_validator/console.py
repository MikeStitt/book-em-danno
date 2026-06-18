"""`ConsoleReporter` — renders a `danno validate` run to the terminal.

The view half of the run: it turns the orchestrator's plan, per-tier
`ValidateEvent`s, and final `ValidateResult` into the live status + summary the UX
doc describes. Kept separate from `run.py` (the logic) so the orchestration stays
unit-testable with a no-op `Reporter`; this rendering is plain I/O over the shared
`console` and is not unit-tested.
"""

from __future__ import annotations

from collections import Counter

from book_em_danno.core.exec import console
from danno_validator.baseline import BASELINE_MODEL
from danno_validator.events import ValidateEvent
from danno_validator.menu import is_recommended
from danno_validator.report import verdict_label
from danno_validator.run import Reporter, ValidatePlan, ValidateResult
from danno_validator.sweep import SweepResult


class ConsoleReporter(Reporter):
    """Stream a validate run to `console`: plan preamble, per-tier status, summary."""

    def plan(self, plan: ValidatePlan, *, dry_run: bool) -> None:
        title = (
            "plan (--dry-run; drop --dry-run to execute)"
            if dry_run
            else "plan (validate runs immediately; --dry-run previews without running)"
        )
        console.print(f"\n[bold]danno validate — {title}[/bold]\n")
        swept = ", ".join(plan.swept_models) or "(none)"
        declared = ", ".join(plan.declared_models) or "(none)"
        tiers = " · ".join(f"L{n}" for n in range(plan.max_level + 1))
        baseline = (
            f"Claude Code @ {plan.baseline_model or '(default model)'}" if plan.baseline else "off"
        )
        rows = [
            ("config", str(plan.config_path)),
            ("declared", declared),
            ("sweeping", swept),
            ("tiers", f"{tiers}  (--max-level {plan.max_level})"),
            ("baseline", baseline),
            ("workspace", f"{plan.workspace}   (throwaway, validator-owned)"),
            ("report", str(plan.out_dir)),
        ]
        for label, value in rows:
            console.print(f"  {label:11s} {value}")
        console.print(f"\n  disposable sandbox (sweep):    {plan.sweep_sandbox}")
        if plan.baseline_sandbox is not None:
            console.print(f"  disposable sandbox (baseline): {plan.baseline_sandbox}")
        console.print(
            "\n  [yellow]⚠[/yellow] local models run sequentially and are slow; the "
            "baseline makes paid API calls.\n    Your project is not modified — the "
            "battery runs in the throwaway workspace above."
        )
        if dry_run:
            console.print("\nDrop --dry-run to provision, sweep, and write the report.")

    def phase(self, text: str) -> None:
        console.print(f"\n[bold]▶[/bold] {text}")

    def event(self, ev: ValidateEvent) -> None:
        if ev.phase == "config-start":
            console.print(f"\n[bold]▶[/bold] {ev.config}   [dim]{ev.model_ref}[/dim]")
        elif ev.phase == "tier-start":
            console.print(f"    L{ev.level} {ev.label} … [dim]running[/dim]")
        elif ev.phase == "tier-done":
            label = verdict_label(ev.overall) if ev.overall is not None else "?"
            tok = f", {ev.tokens} tok" if ev.tokens else ""
            console.print(f"    L{ev.level}  {label}   [dim]({ev.latency_s:.1f}s{tok})[/dim]")
        elif ev.phase == "tier-skip":
            console.print(f"    [dim]→ L{ev.level} {ev.label} skipped ({ev.reason})[/dim]")

    def summary(self, result: ValidateResult) -> None:
        console.print("\n[bold]── results ──[/bold]")
        console.print(f"  {'config':14s} {'L0':18s} {'L1':18s} {'L2':18s}")
        swept: list[SweepResult] = []
        for s in result.results:
            is_base = s.variant.model_name == BASELINE_MODEL
            if not is_base:
                swept.append(s)
            l0 = verdict_label(s.result.overall)
            l1 = verdict_label(s.level1.overall) if s.level1 is not None else "—"
            l2 = verdict_label(s.level2.overall) if s.level2 is not None else "—"
            name = f"{s.variant.model_name}{' (base)' if is_base else ''}"
            console.print(f"  {name:14s} {l0:18s} {l1:18s} {l2:18s}")

        passed_l0 = sum(1 for s in swept if s.result.passed)
        passed_all = sum(1 for s in swept if is_recommended(s))
        taxonomy = Counter(s.result.overall.value for s in swept)
        tax = " · ".join(f"{k} {v}" for k, v in sorted(taxonomy.items()))
        console.print(
            f"\n  swept: {len(swept)} config(s) · {passed_l0} cleared L0 · "
            f"{passed_all} cleared all tiers"
        )
        if tax:
            console.print(f"  taxonomy: {tax}")
        base = next((s for s in result.results if s.variant.model_name == BASELINE_MODEL), None)
        if base is not None:
            verdict = "cleared all tiers" if is_recommended(base) else "did not clear all tiers"
            console.print(f"  baseline: {base.variant.model_ref} {verdict} (reference)")

        if result.index is not None:
            console.print(f"\n  report   {result.index}")
        if result.menu_path is not None:
            console.print(f"  menu     {result.menu_path}")
            console.print(
                # Escape the literal brackets so rich doesn't parse `[agents]` as markup.
                r"           [dim]↳ uncomment the \[agents] line you want and copy it into "
                "your danno.toml (validate never edits it)[/dim]"
            )
        if result.results_json is not None:
            console.print(f"  results  {result.results_json}")
        if result.strict_failed:
            console.print(
                "\n  [red]✗ --strict: a swept config did not clear its requested tiers[/red]"
            )
