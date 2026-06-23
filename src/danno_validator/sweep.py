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
from danno_validator.driver import Turn, TurnFn, opencode_run, reset_workspace, seed_workspace
from danno_validator.events import ProgressFn, ValidateEvent
from danno_validator.judge import JudgeFn
from danno_validator.level0 import DEFAULT_AGENT, DEFAULT_SCRIPT, ConversationResult, ScriptedTurn
from danno_validator.level1 import DEFAULT_TASK as DEFAULT_L1_TASK
from danno_validator.level1 import Level1Task, TaskResult, run_level1
from danno_validator.level2 import DEFAULT_TASK as DEFAULT_L2_TASK
from danno_validator.level2 import DevTaskResult, Level2Task, run_level2
from danno_validator.matrix import ConfigVariant, model_variants

# Identity stamped on the seed commit so `prepare_workspace` never depends on the
# host's global git user config (which may be unset in CI).
_GIT_AUTHOR = ("user.name=danno-validator", "user.email=danno-validator@local")


@dataclass
class SweepResult:
    """One config's place in the sweep: the variant and its tiered outcomes.

    `result` is the Level-0 verdict; `level1`/`level2` are the higher-tier
    verdicts, each present only when the *previous* tier passed and that tier was
    requested — `None` means it was skipped (the L0→L1→L2 short-circuit: a config
    that fails an earlier tier never wastes time on a later one).
    """

    variant: ConfigVariant
    result: ConversationResult
    level1: TaskResult | None = None
    level2: DevTaskResult | None = None


def prepare_workspace(runner: Runner, workspace_root: Path, config: DannoConfig) -> Path:
    """Make `workspace_root` a clean, validator-owned git repo carrying the base
    `opencode.jsonc`, ready for `run_sweep`'s per-variant resets. Returns the path.

    Idempotent: re-running re-seeds the marker, regenerates the config (a no-op when
    unchanged), and re-commits only if something changed. The generated config is
    **committed** so `reset_workspace` (`git clean -fdx && git reset --hard`)
    preserves it across runs instead of deleting it as untracked.
    """
    seed_workspace(workspace_root)
    # disable_title: switch off opencode's per-session thread-title generator for the
    # sweep. Verified on the wire that it otherwise fires one chat/completions per
    # session against the local default model — wasted local compute for a throwaway
    # battery (a cloud-only sweep would still spin up the big local model just to title).
    generate(config, workspace_root, apply=True, disable_title=True)
    ws = str(workspace_root)
    author_flags = [arg for kv in _GIT_AUTHOR for arg in ("-c", kv)]
    # `init` is idempotent; `add` always stages the marker + config; `commit` is
    # allowed to no-op (check=False) so re-preparing an unchanged repo doesn't error.
    runner.capture(["git", "-C", ws, "init"], check=True)
    runner.capture(["git", "-C", ws, "add", "-A"], check=True)
    runner.capture(["git", "-C", ws, *author_flags, "commit", "-m", "seed validator workspace"])
    return workspace_root


def _authed_opencode_run(env_file: Path) -> TurnFn:
    """A `TurnFn` that drives `opencode_run` with `env_file` bound.

    Mirrors the baseline's `_authed_claude_run`: it keeps the credentials env-file
    out of the agent-agnostic level-runner / `run_tiers` API so the runners just
    call a plain `TurnFn`. The sweep binds the file once (it carries every cloud
    config's keys) and every turn execs `opencode` with `--env-file` so anthropic /
    NVIDIA / … backends authenticate. Local Ollama models ignore it.
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
        return opencode_run(
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
    level1_task: Level1Task = DEFAULT_L1_TASK,
    level2: bool = True,
    level2_task: Level2Task = DEFAULT_L2_TASK,
    env_file: Path | None = None,
    judge: JudgeFn | None = None,
    on_event: ProgressFn | None = None,
) -> list[SweepResult]:
    """Run the tiered battery against each model variant of `config`, sequentially.

    `only` restricts the swept models (see `matrix.model_variants`). When `reset`
    (the default), the validator-owned `workspace_root` is reset to its committed
    baseline before each variant via the guarded `reset_workspace`, so one config's
    side effects never leak into the next. The tiers run as a short-circuit chain:
    each variant runs L0, then L1 **only if L0 passed** (`level1`), then L2 **only if
    L1 passed** (`level2`) — the plan's tiering, so a config that stalls early never
    wastes a run on a later tier (the skipped tier's `SweepResult` field stays
    `None`). The higher tiers need no extra reset: each task seeds its own clean
    state surgically.

    `env_file`, when set, is a chmod-600 file of credentials (cloud configs' API
    keys) bound into every opencode exec via `--env-file` — without it a swept
    anthropic/NVIDIA/… model errors at L0 for missing auth (local Ollama models
    need none). When `None`, the runners resolve `opencode_run` at call time as
    before. Returns one `SweepResult` per variant, in matrix order.
    """
    run_turn = _authed_opencode_run(env_file) if env_file is not None else None
    results: list[SweepResult] = []
    for variant in model_variants(config, only=only):
        if reset:
            reset_workspace(runner, sandbox, workspace_root)
        results.append(
            run_tiers(
                runner,
                sandbox,
                variant=variant,
                workspace_root=workspace_root,
                agent=agent,
                script=script,
                level1=level1,
                level1_task=level1_task,
                level2=level2,
                level2_task=level2_task,
                run_turn=run_turn,
                judge=judge,
                on_event=on_event,
            )
        )
    return results


def run_tiers(
    runner: Runner,
    sandbox: str,
    *,
    variant: ConfigVariant,
    workspace_root: Path,
    agent: str = DEFAULT_AGENT,
    script: tuple[ScriptedTurn, ...] = DEFAULT_SCRIPT,
    level1: bool = True,
    level1_task: Level1Task = DEFAULT_L1_TASK,
    level2: bool = True,
    level2_task: Level2Task = DEFAULT_L2_TASK,
    judge: JudgeFn | None = None,
    run_turn: TurnFn | None = None,
    on_event: ProgressFn | None = None,
) -> SweepResult:
    """Run the tiered L0→L1→L2 short-circuit for one `variant`, returning its result.

    The shared core of both the model sweep (`run_sweep`, one call per model
    variant with the default opencode `run_turn`) and the Claude baseline
    (`baseline.run_baseline`, one call with `run_turn=driver.claude_run`). It runs
    **no reset** — workspace isolation is the caller's job (the sweep resets per
    variant). Each tier runs only if the previous passed: L0, then L1 if L0 passed
    (`level1`), then L2 if L1 passed (`level2`); a skipped tier's field stays
    `None`. `variant.model_ref` is passed as the model to the turn producer
    (opencode uses it for `-m`; `claude_run` ignores it).

    `on_event`, when given, receives a `ValidateEvent` at each config/tier boundary
    (start, done-with-verdict, or skip) for live status reporting — purely
    observational; the returned `SweepResult` is identical whether or not anyone is
    watching.

    `judge`, when given, grades L2 dev quality on top of the objective oracle (see
    `judge.make_judge`); the verdict rides on `SweepResult.level2.judgement`. Off by
    default so the sweep stays offline unless a caller wires in a real judge client.
    """

    def emit(**kw: object) -> None:
        if on_event is not None:
            on_event(ValidateEvent(config=variant.model_name, model_ref=variant.model_ref, **kw))  # type: ignore[arg-type]

    emit(phase="config-start")
    emit(phase="tier-start", level=0, label="liveness")
    result = level0.run_level0(
        runner,
        sandbox,
        model=variant.model_ref or None,  # empty ref ⇒ no -m (config carries the model)
        workspace_root=workspace_root,
        agent=agent,
        script=script,
        run_turn=run_turn,
    )
    emit(
        phase="tier-done",
        level=0,
        label="liveness",
        overall=result.overall,
        passed=result.passed,
        latency_s=result.total_latency_s,
        tokens=result.total_tokens,
    )
    l1: TaskResult | None = None
    if level1:
        if result.passed:
            emit(phase="tier-start", level=1, label="tool/bash")
            l1 = run_level1(
                runner,
                sandbox,
                model=variant.model_ref or None,  # empty ref ⇒ no -m (config carries the model)
                workspace_root=workspace_root,
                task=level1_task,
                agent=agent,
                run_turn=run_turn,
            )
            emit(
                phase="tier-done",
                level=1,
                label="tool/bash",
                overall=l1.overall,
                passed=l1.passed,
                latency_s=l1.latency_s,
                tokens=l1.tokens,
            )
        else:
            emit(phase="tier-skip", level=1, label="tool/bash", reason="L0 did not pass")
    l2: DevTaskResult | None = None
    if level2:
        if l1 is not None and l1.passed:
            emit(phase="tier-start", level=2, label="software-dev")
            l2 = run_level2(
                runner,
                sandbox,
                model=variant.model_ref or None,  # empty ref ⇒ no -m (config carries the model)
                workspace_root=workspace_root,
                task=level2_task,
                agent=agent,
                run_turn=run_turn,
                judge=judge,
            )
            emit(
                phase="tier-done",
                level=2,
                label="software-dev",
                overall=l2.overall,
                passed=l2.passed,
                latency_s=l2.latency_s,
                tokens=l2.tokens,
            )
        else:
            emit(
                phase="tier-skip",
                level=2,
                label="software-dev",
                reason="an earlier tier did not pass",
            )
    emit(phase="config-done")
    return SweepResult(variant=variant, result=result, level1=l1, level2=l2)
