"""Claude Code baseline: run the *same* tiered battery against `claude` headless.

M5's reference point. The local-model sweep (`sweep.run_sweep`) answers *which
declared models work*; the baseline answers *how the strong agent does on this
exact battery*, so a local model's L0/L1/L2 verdicts can be read against it. The
comparison is on **agent-agnostic oracle outcomes** — the workspace side-effect
probe (L0), the `line_count.txt` check (L1), and the hidden test suite run in-VM
(L2) — so it sidesteps opencode-vs-claude transcript differences entirely.

It reuses the whole battery unchanged via two seams: `driver.claude_run` (the
in-sandbox `claude -p --output-format stream-json` turn producer, injected as the
level runners' `run_turn`) and `sweep.run_tiers` (the shared L0→L1→L2
short-circuit). The result is a single `SweepResult` carrying a synthetic
`claude-code` variant, so the existing reporter renders it as just another matrix
row + per-config page (see `report.render_matrix_index`).

The baseline drives a **claude** sandbox (`docker sandbox create claude …`) over
the same validator-owned workspace mount the opencode sweep used; claude ignores
the generated `.opencode/opencode.jsonc`, and `claude_run` ignores the variant's
`model_ref`/`agent` (the baseline is the fixed default Claude config).
"""

from __future__ import annotations

from pathlib import Path

from book_em_danno.commands.sandbox import DEFAULT_OLLAMA_URL, _build_env_file, agent_env
from book_em_danno.core.exec import Runner
from danno_validator.driver import Turn, TurnFn, claude_run, reset_workspace
from danno_validator.events import ProgressFn
from danno_validator.level0 import DEFAULT_SCRIPT, ScriptedTurn
from danno_validator.level1 import DEFAULT_TASK as DEFAULT_L1_TASK
from danno_validator.level1 import Level1Task
from danno_validator.level2 import DEFAULT_TASK as DEFAULT_L2_TASK
from danno_validator.level2 import Level2Task
from danno_validator.matrix import ConfigVariant
from danno_validator.sweep import SweepResult, run_tiers

# The synthetic "model" name the baseline row carries in the results matrix; the
# reporter keys off it to flag the row as the Claude Code reference.
BASELINE_MODEL = "claude-code"


def baseline_variant(model: str | None = None) -> ConfigVariant:
    """The synthetic `ConfigVariant` identifying the Claude Code baseline row.

    `model` is the claude model the row used (the pinned alias/id, or the actual
    resolved model once known — see `run_baseline`). It becomes the row's
    `model_ref`, so the results matrix records *which* claude model the baseline
    ran, exactly as it records `ollama/…` for a local config. `model_name` stays
    `BASELINE_MODEL` so the reporter still flags the row and excludes it from the
    swept-config tally.
    """
    shown = model or "(default model)"
    return ConfigVariant(
        model_name=BASELINE_MODEL,
        model_ref=shown,
        description=f"Claude Code headless baseline ({shown})",
    )


def _build_claude_auth_env_file() -> Path:
    """Build a chmod-600 env-file carrying claude's auth, for the exec `--env-file`.

    Reuses danno's own secret handling: `agent_env("claude", …)` resolves
    `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` from the host environment and
    fails loud when neither is set (Working Rule 8); `_build_env_file` writes them
    to a 0600 temp file. The caller is responsible for unlinking it.
    """
    return _build_env_file(agent_env("claude", DEFAULT_OLLAMA_URL), [], [])


def _authed_claude_run(env_file: Path, claude_model: str | None) -> TurnFn:
    """A `TurnFn` that drives `claude_run` with `env_file` and `claude_model` bound.

    Keeps the auth env-file and the pinned claude model out of the agent-agnostic
    level-runner / `run_tiers` API: the baseline binds them here and the runners
    just call a plain `TurnFn`. The `model` the runner passes (`variant.model_ref`,
    a display string) is **ignored** — the bound `claude_model` (the real alias/id
    or None for the default) is what reaches claude's `--model`.
    """

    def run(
        runner: Runner,
        name: str,
        prompt: str,
        *,
        session: str | None = None,
        agent: str | None = None,
        model: str | None = None,
        skip_permissions: bool = False,
        workspace: str | Path | None = None,
    ) -> Turn:
        return claude_run(
            runner,
            name,
            prompt,
            session=session,
            agent=agent,
            model=claude_model,
            skip_permissions=skip_permissions,
            workspace=workspace,
            env_file=env_file,
        )

    return run


def _record_actual_model(result: SweepResult, *, requested: str | None) -> None:
    """Overwrite the baseline row's model with the one claude actually resolved.

    Reads ground truth from the L0 turn (`ClaudeTurn.model`, from claude's `system`
    init event) so the matrix shows e.g. `claude-opus-4-8[1m]` even when the model
    was left to the default; falls back to the `requested` alias if claude reported
    none. Mutating the freshly-built `SweepResult` (its `variant` and the L0
    `result.model`) keeps the matrix row and the per-config page in agreement.
    """
    records = result.result.records
    reported = getattr(records[0].turn, "model", None) if records else None
    shown = reported or requested
    if shown:
        result.variant = baseline_variant(shown)
        result.result.model = shown


def run_baseline(
    runner: Runner,
    sandbox: str,
    *,
    workspace_root: Path,
    model: str | None = None,
    reset: bool = True,
    script: tuple[ScriptedTurn, ...] = DEFAULT_SCRIPT,
    level1: bool = True,
    level1_task: Level1Task = DEFAULT_L1_TASK,
    level2: bool = True,
    level2_task: Level2Task = DEFAULT_L2_TASK,
    on_event: ProgressFn | None = None,
) -> SweepResult:
    """Run the L0→L1→L2 battery against Claude Code in a claude `sandbox`.

    Drives the identical scripts/tasks the model sweep uses, via
    `driver.claude_run`, and returns one `SweepResult` (the `baseline_variant`)
    ready to append to the sweep's results for a combined report. When `reset`
    (the default), the validator-owned `workspace_root` is reset to its committed
    baseline first via the guarded `reset_workspace` — so the baseline starts from
    the same clean state as each swept model.

    `model` pins claude's model (`--model`; an alias like "opus"/"sonnet" or a full
    id) — the counterpart of the sweep's per-model `-m`, so the baseline controls
    which model runs rather than inheriting the install default (which varies in
    cost/latency). Whether pinned or default, the **actual** resolved model claude
    reports is recorded as the row's model, so the matrix always tracks what ran.

    Claude auth (`CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`) must be in the host
    environment: it is built into a chmod-600 env-file passed to each `claude`
    exec (the file is removed afterward), so a missing token fails loud up front.
    """
    auth_file = _build_claude_auth_env_file()
    try:
        if reset:
            reset_workspace(runner, sandbox, workspace_root)
        result = run_tiers(
            runner,
            sandbox,
            variant=baseline_variant(model),
            workspace_root=workspace_root,
            script=script,
            level1=level1,
            level1_task=level1_task,
            level2=level2,
            level2_task=level2_task,
            run_turn=_authed_claude_run(auth_file, model),
            on_event=on_event,
        )
    finally:
        auth_file.unlink(missing_ok=True)
    _record_actual_model(result, requested=model)
    return result
