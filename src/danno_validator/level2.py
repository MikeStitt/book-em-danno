"""Level-2 software-dev battery: a small repo task with a hidden test suite oracle.

Where Level 1 asks *can the agent use tools to produce a specific file*, Level 2
asks *can it make a real code change that a test suite accepts*. A task seeds a
tiny repo — a source stub the agent must implement against a written contract —
and the oracle is a **hidden test suite**: the test is never seeded into the
workspace (so the agent can't read or hardcode against it), it is written in only
at grading time and **run inside the sandbox** (the repo lives in the mounted
workspace and the toolchain is the VM's, per the opencode-only-in-sandbox
invariant — the test *run* is the oracle, so it belongs in the VM too). Tests
passing is an objective, deterministic verdict — no LLM judge (one arrives in a
later milestone only for fuzzy partial-credit grading on top of this backbone).

The "tests pass" boolean is fed into the *same* pure `classify_turn` the L0/L1
oracles use, so an L2 result lands in the existing `FailureClass` taxonomy
(`early-stop` when a tool ran but the tests still fail, `malformed-tool-args` when
a tool errored, `stall`/`hallucinated-tool` when it talked but never edited, …) —
no L2-only class is needed, exactly as M3 found for L1. The richer L2 record (the
captured test run) lives on `DevTaskResult`, not in the taxonomy.

`run_level2` performs I/O (seeds the repo, drives the sandbox via the injected
`Runner`, runs the hidden tests in the VM); the judgement is the pure oracle's, so
the decision logic stays unit-testable. Seeding is **surgical** — it writes the
task's source files and removes only its own (hidden) test file, never a
destructive git reset, so it is safe against any workspace.

Default = one curated task (`DEFAULT_TASK`); a larger bank / `--full` and the
general benchmark-adapter path (Aider-polyglot / Exercism repo+tests) are later
milestones (deliberately not full SWE-bench, whose per-task docker harness would
mean nested virtualization inside the sandbox VM).
"""

from __future__ import annotations

import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from book_em_danno.core.exec import CommandFailedError, Runner
from danno_validator.driver import Turn, TurnFn, capture_exec, opencode_run
from danno_validator.judge import DevWork, JudgeFn, Judgement
from danno_validator.level0 import DEFAULT_AGENT
from danno_validator.oracle import FailureClass, TurnVerdict, classify_turn

# `bash -lc` returns 127 when the command itself can't be found — i.e. the test
# runtime (e.g. python3) is absent from the sandbox image, not a test failure.
# That is a harness/environment misconfiguration, so we fail loud rather than
# silently score every model as failing the suite (Working Rule 8).
_RUNTIME_MISSING_EXIT = 127


@dataclass(frozen=True)
class Level2Task:
    """A declarative software-dev task graded by a hidden test suite.

    `sources` are `(filename, content)` pairs seeded into the workspace as the
    starting repo (a stub the agent must implement against `prompt`). `test_file`
    /`test_content` are the **hidden** test: never seeded, written in only at
    grading time so the agent works from the contract alone. `test_command` runs
    the suite inside the sandbox and **exit code 0 means pass** (so it works with
    plain `python3 t.py`, `node t.js`, `pytest`, … — any runner with that
    convention). Keeping the task declarative makes the oracle objective and the
    spec trivial to unit-test.
    """

    label: str
    prompt: str
    sources: tuple[tuple[str, str], ...]
    test_file: str
    test_content: str
    test_command: str

    def seed(self, workspace_root: Path) -> None:
        """Write the task's source files and remove any stale hidden test.

        Surgical (Working Rule 3/6): touches only this task's own files, so it
        never needs a destructive git reset and is safe on a borrowed workspace.
        Removing `test_file` keeps the suite genuinely *hidden* during the agent's
        turn — it can't be read for hints or hardcoded against, and a leftover from
        a prior run can't fake a pass.
        """
        for name, content in self.sources:
            path = workspace_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (workspace_root / self.test_file).unlink(missing_ok=True)

    def write_test(self, workspace_root: Path) -> None:
        """Write the hidden test into the workspace, just before grading."""
        path = workspace_root / self.test_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.test_content)


# The curated default: implement FizzBuzz. A real source edit (the stub raises,
# so nothing passes until the agent writes the logic) with a fully specified,
# deterministic contract — chosen so the hidden suite can be exact and the task
# genuinely requires editing code rather than producing a single literal value.
_FIZZBUZZ_STUB = '''\
def fizzbuzz(n):
    """Return the FizzBuzz string for the positive integer ``n`` (see the task)."""
    raise NotImplementedError
'''

_FIZZBUZZ_TEST = """\
import sys

from fizzbuzz import fizzbuzz

CASES = {
    1: "1", 2: "2", 3: "Fizz", 4: "4", 5: "Buzz", 6: "Fizz",
    9: "Fizz", 10: "Buzz", 15: "FizzBuzz", 30: "FizzBuzz", 23: "23", 45: "FizzBuzz",
}

failures = []
for n, want in CASES.items():
    got = fizzbuzz(n)
    if got != want:
        failures.append(f"fizzbuzz({n}) == {got!r}, expected {want!r}")

if failures:
    print("\\n".join(failures))
    sys.exit(1)
print(f"ok — {len(CASES)} cases passed")
"""

DEFAULT_TASK = Level2Task(
    label="fizzbuzz",
    prompt=(
        "Implement the `fizzbuzz` function in the file `fizzbuzz.py` in the current "
        "project directory. It takes a positive integer `n` and returns a string: "
        '"Fizz" if `n` is divisible by 3, "Buzz" if divisible by 5, "FizzBuzz" if '
        "divisible by both 3 and 5, and otherwise the number itself as a string "
        '(e.g. "7"). Edit the file with your tools now; keep the function name and '
        "signature unchanged."
    ),
    sources=(("fizzbuzz.py", _FIZZBUZZ_STUB),),
    test_file="hidden_test_fizzbuzz.py",
    test_content=_FIZZBUZZ_TEST,
    test_command="python3 hidden_test_fizzbuzz.py",
)


@dataclass
class TestRun:
    """The captured result of running a task's hidden test suite in the sandbox."""

    # Not a pytest test case despite the `Test*` name — keep pytest from collecting it.
    __test__ = False

    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def passed(self) -> bool:
        """True iff the suite exited 0 (the pass convention for `test_command`)."""
        return self.returncode == 0


@dataclass
class DevTaskResult:
    """The Level-2 outcome for one config: the turn, its verdict, and the test run.

    Mirrors L1's `TaskResult` but carries the richer L2 record (`test_run`) — the
    objective hidden-suite result that backs the verdict — since "which tests ran"
    is more than a single boolean. The `FailureClass` tag still comes from the
    shared `classify_turn`, so L2 needs no new taxonomy entry.
    """

    model: str
    sandbox: str
    workspace_root: Path
    task_label: str
    session_id: str | None
    turn: Turn
    verdict: TurnVerdict
    test_run: TestRun
    latency_s: float = field(default=0.0)
    # The fuzzy dev-quality verdict, layered on top of the objective `verdict`.
    # `None` when no judge was supplied (the default — keeps `ninja check` offline).
    judgement: Judgement | None = field(default=None)

    @property
    def passed(self) -> bool:
        return self.verdict.passed

    @property
    def overall(self) -> FailureClass:
        return self.verdict.failure_class

    @property
    def tokens(self) -> int:
        return self.turn.tokens

    @property
    def cost(self) -> float:
        return self.turn.cost


def run_tests(runner: Runner, sandbox: str, *, workspace_root: Path, task: Level2Task) -> TestRun:
    """Write the hidden test in and run it **inside** `sandbox`, returning the run.

    The repo lives in the mounted workspace and the toolchain is the VM's, so the
    suite runs in-VM via `capture_exec` (the test run is the oracle and stays under
    the opencode-only-in-sandbox invariant). The test is written host-side into the
    bidirectional mount, then `cd`-run at the workspace root. A 127 exit means the
    test runtime itself is missing from the image — a misconfiguration, surfaced
    loudly rather than miscounted as a model failure.
    """
    task.write_test(workspace_root)
    command = f"cd {shlex.quote(str(workspace_root))} && {task.test_command}"
    res = capture_exec(runner, sandbox, command)
    if res.returncode == _RUNTIME_MISSING_EXIT:
        raise CommandFailedError(
            f"the Level-2 test runtime is missing in sandbox {sandbox!r}: "
            f"`{task.test_command}` exited 127 (command not found). "
            f"stderr: {res.stderr.strip() or '(empty)'}"
        )
    return TestRun(
        command=task.test_command,
        returncode=res.returncode,
        stdout=res.stdout,
        stderr=res.stderr,
    )


def _read_produced(workspace_root: Path, task: Level2Task) -> tuple[tuple[str, str | None], ...]:
    """Read back each task source file the agent may have edited (None if absent).

    The workspace is the bidirectional mount, so the produced code is readable
    host-side — this is what the dev-quality judge grades. Reading happens after
    `run_tests` writes the hidden test in, but that never touches the source files.
    """
    produced: list[tuple[str, str | None]] = []
    for name, _stub in task.sources:
        path = workspace_root / name
        produced.append((name, path.read_text() if path.exists() else None))
    return tuple(produced)


def run_level2(
    runner: Runner,
    sandbox: str,
    *,
    model: str | None,
    workspace_root: Path,
    task: Level2Task = DEFAULT_TASK,
    agent: str = DEFAULT_AGENT,
    run_turn: TurnFn | None = None,
    judge: JudgeFn | None = None,
) -> DevTaskResult:
    """Run one Level-2 dev task against `model` in `sandbox`, returning the result.

    Single-shot: the repo is seeded, one headless turn drives the agent to edit the
    source (`--agent build` in the workspace root via `-w`, the M1-verified shape),
    then the hidden suite runs in the VM and its pass/fail becomes the `side_effect`
    fed to the pure `classify_turn`. Tests passing → `pass`; a clean edit that
    leaves the suite failing → `early-stop`; a talk-no-edit turn → `stall`/etc.

    `run_turn` is the turn producer — `opencode_run` by default (resolved at call
    time so a monkeypatched `level2.opencode_run` still applies); the Claude
    baseline passes `driver.claude_run`.

    `judge`, when supplied, grades the produced code's *quality* on top of the
    objective test verdict (clarity, over-/under-build) and the result is attached
    as `DevTaskResult.judgement`. It's off by default so the harness — and
    `ninja check` — stay offline; the live CLI wires in an `AnthropicJudgeClient`.
    """
    turn_fn = run_turn or opencode_run
    task.seed(workspace_root)
    start = time.monotonic()
    turn = turn_fn(
        runner,
        sandbox,
        task.prompt,
        agent=agent,
        model=model,
        skip_permissions=True,
        workspace=workspace_root,
    )
    latency = time.monotonic() - start
    test_run = run_tests(runner, sandbox, workspace_root=workspace_root, task=task)
    verdict = classify_turn(turn, side_effect=test_run.passed, expects_action=True)
    judgement = None
    if judge is not None:
        work = DevWork(
            prompt=task.prompt,
            sources=_read_produced(workspace_root, task),
            test_passed=test_run.passed,
            test_output=(test_run.stdout + test_run.stderr).strip(),
        )
        judgement = judge(work)
    return DevTaskResult(
        model=model or "",
        sandbox=sandbox,
        workspace_root=workspace_root,
        task_label=task.label,
        session_id=turn.session_id,
        turn=turn,
        verdict=verdict,
        test_run=test_run,
        latency_s=latency,
        judgement=judgement,
    )
