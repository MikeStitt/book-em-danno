"""Sequentially run the Level-0 battery across a matrix of model variants.

This is M2's orchestrator. Given a base `DannoConfig` and one **validator-owned**
workspace mounted into a sandbox, it:

1. `prepare_workspace` — seeds the ownership marker, generates the base
   `.opencode/opencode.jsonc` (declaring every candidate model), and commits the
   result to a fresh git repo so the config survives `reset_workspace`'s guarded
   `git clean -fdx && git reset --hard`;
2. `run_sweep` — for each model variant (`matrix.model_variants`), resets the
   workspace to that clean baseline and runs the Level-0 conversation against the
   model via OpenCode's `-m` ref, collecting one `ConversationResult` apiece.

Local models are large (tens of GB resident), so the sweep is **sequential** by
design — there is no concurrency to win when only one model fits in RAM at a time.

Provisioning the sandbox itself (`book_em_danno.commands.sandbox.provision`) is the
caller's job; the sweep assumes a ready sandbox whose mount *is* `workspace_root`,
so the guarded reset applies and configs are isolated (the M1→M2 prerequisite).
The host-side git/generate setup goes through the injected `Runner`, so the whole
orchestration is unit-testable without a Docker daemon.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from book_em_danno.config.generate import generate
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import Runner
from danno_validator import level0
from danno_validator.driver import reset_workspace, seed_workspace
from danno_validator.level0 import DEFAULT_AGENT, DEFAULT_SCRIPT, ConversationResult, ScriptedTurn
from danno_validator.level1 import DEFAULT_TASK, Level1Task, TaskResult, run_level1
from danno_validator.matrix import ConfigVariant, model_variants

# Identity stamped on the seed commit so `prepare_workspace` never depends on the
# host's global git user config (which may be unset in CI).
_GIT_AUTHOR = ("user.name=danno-validator", "user.email=danno-validator@local")


@dataclass
class SweepResult:
    """One config's place in the sweep: the variant and its tiered outcomes.

    `result` is the Level-0 verdict; `level1` is the Level-1 verdict, present only
    when L0 passed and L1 was requested — `None` means L1 was skipped (the tiering
    short-circuit: a config that fails L0 never wastes time on L1).
    """

    variant: ConfigVariant
    result: ConversationResult
    level1: TaskResult | None = None


def prepare_workspace(runner: Runner, workspace_root: Path, config: DannoConfig) -> Path:
    """Make `workspace_root` a clean, validator-owned git repo carrying the base
    `opencode.jsonc`, ready for `run_sweep`'s per-variant resets. Returns the path.

    Idempotent: re-running re-seeds the marker, regenerates the config (a no-op when
    unchanged), and re-commits only if something changed. The generated config is
    **committed** so `reset_workspace` (`git clean -fdx && git reset --hard`)
    preserves it across runs instead of deleting it as untracked.
    """
    seed_workspace(workspace_root)
    generate(config, workspace_root, apply=True)
    ws = str(workspace_root)
    author_flags = [arg for kv in _GIT_AUTHOR for arg in ("-c", kv)]
    # `init` is idempotent; `add` always stages the marker + config; `commit` is
    # allowed to no-op (check=False) so re-preparing an unchanged repo doesn't error.
    runner.capture(["git", "-C", ws, "init"], check=True)
    runner.capture(["git", "-C", ws, "add", "-A"], check=True)
    runner.capture(["git", "-C", ws, *author_flags, "commit", "-m", "seed validator workspace"])
    return workspace_root


def run_sweep(
    runner: Runner,
    sandbox: str,
    *,
    config: DannoConfig,
    workspace_root: Path,
    only: Sequence[str] | None = None,
    agent: str = DEFAULT_AGENT,
    reset: bool = True,
    script: tuple[ScriptedTurn, ...] = DEFAULT_SCRIPT,
    level1: bool = True,
    level1_task: Level1Task = DEFAULT_TASK,
) -> list[SweepResult]:
    """Run the tiered battery against each model variant of `config`, sequentially.

    `only` restricts the swept models (see `matrix.model_variants`). When `reset`
    (the default), the validator-owned `workspace_root` is reset to its committed
    baseline before each variant via the guarded `reset_workspace`, so one config's
    side effects never leak into the next. When `level1` (the default), each variant
    that **passes L0** then runs the Level-1 tool/bash task — the plan's tiering, so a
    config that stalls at L0 doesn't waste a run on L1 (its `SweepResult.level1` stays
    `None`). L1 needs no extra reset: `level1_task.seed` establishes its own clean
    state surgically. Returns one `SweepResult` per variant, in matrix order.
    """
    results: list[SweepResult] = []
    for variant in model_variants(config, only=only):
        if reset:
            reset_workspace(runner, sandbox, workspace_root)
        result = level0.run_level0(
            runner,
            sandbox,
            model=variant.model_ref,
            workspace_root=workspace_root,
            agent=agent,
            script=script,
        )
        l1: TaskResult | None = None
        if level1 and result.passed:
            l1 = run_level1(
                runner,
                sandbox,
                model=variant.model_ref,
                workspace_root=workspace_root,
                task=level1_task,
                agent=agent,
            )
        results.append(SweepResult(variant=variant, result=result, level1=l1))
    return results
