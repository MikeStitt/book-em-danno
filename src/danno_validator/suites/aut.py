"""Agent-under-test resolution shared by the sweep and the benchmark runner.

Maps an `--agent` name to (1) the prebuilt sandbox image to provision, (2) the
post-provision install step (claurst is not a prebuilt image), and (3) the `TurnFn`
that drives a turn. Keeps the opencode/claurst split in one place so `danno bench`
and `danno validate` resolve the AUT identically.
"""

from __future__ import annotations

from pathlib import Path

from book_em_danno.core.exec import Runner
from danno_validator import claurst
from danno_validator.driver import Turn, TurnFn, opencode_run
from danno_validator.sweep import DEFAULT_AGENT

CLAURST = "claurst"


def resolve_image(agent: str) -> str:
    """The prebuilt sandbox image to `docker sandbox create` for this AUT."""
    return claurst.CLAURST_SANDBOX_IMAGE if agent == CLAURST else agent


def install_aut(runner: Runner, sandbox: str, agent: str) -> None:
    """Post-provision install for AUTs that aren't a prebuilt image (claurst)."""
    if agent == CLAURST:
        claurst.install_claurst(runner, sandbox)


def run_turn_for(agent: str, env_file: Path | None) -> TurnFn:
    """The `TurnFn` driving one turn for this AUT, with `env_file` bound.

    claurst sets up its Ollama relay per turn; opencode is pinned to its read-write
    run-agent (`DEFAULT_AGENT`, "build") so benchmark edits actually land.
    """
    if agent == CLAURST:
        return claurst.authed_claurst_run(env_file)

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
            agent=DEFAULT_AGENT,
            model=model,
            skip_permissions=skip_permissions,
            workspace=workspace,
            env_file=env_file,
        )

    return run
