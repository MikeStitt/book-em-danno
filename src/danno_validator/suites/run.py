"""Run a benchmark suite against one agent-under-test in a provisioned sandbox.

The sandbox-level driver the CLI composes: clone the suite's source (Aider Polyglot
is a git repo), then run each selected task through `seed -> run -> grade`, pointing
the agent's cwd at the task's seeded subdir. Returns the per-task `BenchVerdict`s.
Provisioning the sandbox + installing the AUT + iterating the model matrix is the
caller's job (it reuses the validator's provision/teardown), so this stays a small,
testable loop over already-prepared inputs.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

from book_em_danno.core.exec import Runner
from danno_validator.driver import TurnFn
from danno_validator.suites.aider import AiderTask, load_aider_tasks
from danno_validator.suites.base import BenchVerdict, run_bench_task

_GIT_PREFIX = "git:"


def clone_polyglot(runner: Runner, source: str, dest: Path) -> Path:
    """Shallow-clone the Aider Polyglot `source` (`git:<url>`) into `dest`.

    Returns the checkout dir. Git reaches the host through the sandbox proxy and on
    the host directly (M0 spike). Reuses an existing non-empty checkout so repeated
    runs don't re-clone. Fails loud on a non-git source or a clone failure.
    """
    if not source.startswith(_GIT_PREFIX):
        raise ValueError(f"aider source must be 'git:<url>', got {source!r}")
    if dest.is_dir() and any(dest.iterdir()):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = source[len(_GIT_PREFIX) :]
    runner.capture(["git", "clone", "--depth", "1", url, str(dest)], check=True)
    return dest


def run_aider_suite(
    runner: Runner,
    sandbox: str,
    *,
    checkout: Path,
    select: Sequence[str],
    workspace: Path,
    run_turn: TurnFn,
    model: str | None = None,
) -> list[BenchVerdict]:
    """Run the selected Aider Polyglot exercises against one AUT, in `select` order.

    Each exercise is seeded into its own workspace subdir (`task.provision`), the
    agent is driven with its cwd set to that subdir, and the exercise's own tests
    grade it. `run_turn` is the AUT's turn producer (e.g. `claurst_run` or an authed
    variant); `model` is the model ref passed through to it (the permutation axis).
    """
    tasks: list[AiderTask] = load_aider_tasks(checkout, list(select))
    verdicts: list[BenchVerdict] = []
    for task in tasks:
        task.provision(runner, sandbox, workspace)
        verdicts.append(
            run_bench_task(
                runner,
                sandbox,
                task=task,
                suite="aider",
                workspace=workspace,
                model=model,
                run_turn=cwd_bound(run_turn, task.workspace_dir(workspace)),
            )
        )
    return verdicts


def cwd_bound(run_turn: TurnFn, cwd: Path) -> TurnFn:
    """Wrap a `TurnFn` so every turn runs with its workspace set to `cwd`.

    A benchmark turn must act in the exercise's seeded subdir, not the workspace
    root — so the agent edits and the grader's tests see the same files.
    """

    def run(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        kw["workspace"] = cwd
        return run_turn(runner, name, prompt, **kw)

    return run


def temp_checkout_dir() -> Path:
    """A fresh temp dir for a suite source clone (caller removes it)."""
    return Path(tempfile.mkdtemp(prefix="danno-bench-"))


def remove_checkout(path: Path) -> None:
    """Best-effort cleanup of a temp suite checkout."""
    shutil.rmtree(path, ignore_errors=True)
