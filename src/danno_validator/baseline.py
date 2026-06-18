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


def baseline_variant() -> ConfigVariant:
    """The synthetic `ConfigVariant` identifying the Claude Code baseline row."""
    return ConfigVariant(
        model_name=BASELINE_MODEL,
        model_ref="claude-code (baseline)",
        description="Claude Code headless baseline",
    )


def _build_claude_auth_env_file() -> Path:
    """Build a chmod-600 env-file carrying claude's auth, for the exec `--env-file`.

    Reuses danno's own secret handling: `agent_env("claude", …)` resolves
    `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` from the host environment and
    fails loud when neither is set (Working Rule 8); `_build_env_file` writes them
    to a 0600 temp file. The caller is responsible for unlinking it.
    """
    return _build_env_file(agent_env("claude", DEFAULT_OLLAMA_URL), [], [])


def _authed_claude_run(env_file: Path) -> TurnFn:
    """A `TurnFn` that drives `claude_run` with `env_file` bound for auth.

    Keeps the auth env-file out of the agent-agnostic level-runner / `run_tiers`
    API: the baseline binds it here and the runners just call a plain `TurnFn`.
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
            model=model,
            skip_permissions=skip_permissions,
            workspace=workspace,
            env_file=env_file,
        )

    return run


def run_baseline(
    runner: Runner,
    sandbox: str,
    *,
    workspace_root: Path,
    reset: bool = True,
    script: tuple[ScriptedTurn, ...] = DEFAULT_SCRIPT,
    level1: bool = True,
    level1_task: Level1Task = DEFAULT_L1_TASK,
    level2: bool = True,
    level2_task: Level2Task = DEFAULT_L2_TASK,
) -> SweepResult:
    """Run the L0→L1→L2 battery against Claude Code in a claude `sandbox`.

    Drives the identical scripts/tasks the model sweep uses, via
    `driver.claude_run`, and returns one `SweepResult` (the `baseline_variant`)
    ready to append to the sweep's results for a combined report. When `reset`
    (the default), the validator-owned `workspace_root` is reset to its committed
    baseline first via the guarded `reset_workspace` — so the baseline starts from
    the same clean state as each swept model.

    Claude auth (`CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`) must be in the host
    environment: it is built into a chmod-600 env-file passed to each `claude`
    exec (the file is removed afterward), so a missing token fails loud up front.
    """
    auth_file = _build_claude_auth_env_file()
    try:
        if reset:
            reset_workspace(runner, sandbox, workspace_root)
        return run_tiers(
            runner,
            sandbox,
            variant=baseline_variant(),
            workspace_root=workspace_root,
            script=script,
            level1=level1,
            level1_task=level1_task,
            level2=level2,
            level2_task=level2_task,
            run_turn=_authed_claude_run(auth_file),
        )
    finally:
        auth_file.unlink(missing_ok=True)
