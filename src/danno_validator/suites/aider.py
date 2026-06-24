"""Aider Polyglot suite: self-contained Exercism exercises as `BenchTask`s.

Each exercise (`<lang>/exercises/practice/<slug>/`) ships a stub solution file, a
test file, and instructions — the Exercism layout Aider Polyglot uses. `.meta/
config.json` names which files are the editable `solution` vs the grading `test`.
An `AiderTask` seeds the stub + test into a per-exercise workspace subdir, prompts
the agent with the instructions, and grades by running the exercise's own tests in
the VM. Exercises are self-contained (no heavy deps), so the default isolation is a
shared sandbox with a per-exercise reset of the stub.

M5 verifies the Python lane (pytest); other languages are a `LangSpec` away (their
toolchain must be present/installed in the sandbox). `select` ids are
`"<lang>/<slug>"` (e.g. `"python/anagram"`).
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from book_em_danno.core.exec import Runner
from danno_validator.driver import capture_exec


@dataclass(frozen=True)
class LangSpec:
    """How to install a language's test runtime and run an exercise's tests."""

    install: str | None  # one-time in-VM install (None if the toolchain is present)
    test_command: Callable[[tuple[str, ...]], str]  # given the test files -> shell cmd


# Per-language runtime. Python is M5's verified lane; add a LangSpec to enable more.
LANG_SPECS: dict[str, LangSpec] = {
    "python": LangSpec(
        install="python3 -m pip install --break-system-packages --no-cache-dir pytest",
        test_command=lambda tests: "python3 -m pytest -x -q " + " ".join(map(shlex.quote, tests)),
    ),
    "go": LangSpec(
        install=None,  # the go toolchain must be present in the sandbox image
        test_command=lambda _tests: "go test ./...",
    ),
    "rust": LangSpec(
        install=None,  # cargo must be present
        test_command=lambda _tests: "cargo test",
    ),
}


@dataclass(frozen=True)
class AiderTask:
    """One Aider Polyglot exercise mapped onto the `BenchTask` contract.

    Seeds into a per-exercise subdir of the mounted workspace so exercises never
    collide. `reset` restores the editable stub(s) (the agent may have rewritten
    them) while keeping the seeded test files, so each agent/model variant starts
    from the same clean stub.
    """

    exercise_id: str  # "python/anagram"
    language: str
    instructions: str
    solution_files: tuple[tuple[str, str], ...]  # (relpath, original stub content)
    test_files: tuple[tuple[str, str], ...]  # (relpath, content)
    subdir: str  # workspace-relative dir the exercise is seeded into

    @property
    def id(self) -> str:
        return self.exercise_id

    @property
    def prompt(self) -> str:
        files = ", ".join(p for p, _ in self.solution_files)
        return (
            f"{self.instructions}\n\n"
            f"Implement your solution by editing {files} in the current directory. "
            "Do not edit the test file(s). Make all the tests pass."
        )

    def _dir(self, workspace: Path) -> Path:
        return workspace / self.subdir

    def provision(self, runner: Runner, sandbox: str, workspace: Path) -> None:
        """One-time per sandbox: install the language runtime, then seed all files."""
        spec = LANG_SPECS[self.language]
        if spec.install:
            capture_exec(runner, sandbox, spec.install, check=True)
        d = self._dir(workspace)
        d.mkdir(parents=True, exist_ok=True)
        for relpath, content in (*self.solution_files, *self.test_files):
            target = d / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def reset(self, runner: Runner, sandbox: str, workspace: Path) -> None:
        """Restore the editable stub(s) between agent/model variants (keep tests)."""
        d = self._dir(workspace)
        for relpath, content in self.solution_files:
            (d / relpath).write_text(content, encoding="utf-8")

    def grade(self, runner: Runner, sandbox: str, workspace: Path) -> bool:
        """True iff the exercise's tests pass (exit 0), run in the seeded subdir."""
        spec = LANG_SPECS[self.language]
        tests = tuple(p for p, _ in self.test_files)
        d = self._dir(workspace)
        cmd = f"cd {shlex.quote(str(d))} && {spec.test_command(tests)}"
        return capture_exec(runner, sandbox, cmd, check=False).ok

    def workspace_dir(self, workspace: Path) -> Path:
        """The in-VM cwd a turn should use for this exercise (its seeded subdir)."""
        return self._dir(workspace)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_aider_task(checkout: Path, exercise_id: str) -> AiderTask:
    """Build an `AiderTask` from a polyglot-benchmark checkout for `<lang>/<slug>`.

    Reads `.meta/config.json` (the solution/test file split), the stub solution
    file(s), the test file(s), and the instructions. Fails loud (ValueError) on an
    unknown language, a missing exercise, or a malformed config (Working Rule 8).
    """
    language, _, slug = exercise_id.partition("/")
    if not slug:
        raise ValueError(f"aider exercise id must be '<lang>/<slug>', got {exercise_id!r}")
    if language not in LANG_SPECS:
        raise ValueError(
            f"aider: unsupported language {language!r} (have {sorted(LANG_SPECS)}). "
            "Add a LangSpec to enable it."
        )
    root = checkout / language / "exercises" / "practice" / slug
    config_path = root / ".meta" / "config.json"
    if not config_path.is_file():
        raise ValueError(f"aider: exercise not found or missing config: {config_path}")
    files = json.loads(_read(config_path)).get("files", {})
    solution = files.get("solution") or []
    test = files.get("test") or []
    if not solution or not test:
        raise ValueError(f"aider: {exercise_id} config lists no solution/test files")
    instructions_path = root / ".docs" / "instructions.md"
    instructions = _read(instructions_path) if instructions_path.is_file() else slug
    append = root / ".docs" / "instructions.append.md"
    if append.is_file():
        instructions += "\n\n" + _read(append)
    return AiderTask(
        exercise_id=exercise_id,
        language=language,
        instructions=instructions,
        solution_files=tuple((rel, _read(root / rel)) for rel in solution),
        test_files=tuple((rel, _read(root / rel)) for rel in test),
        subdir=slug,
    )


def load_aider_tasks(checkout: Path, select: list[str]) -> list[AiderTask]:
    """Build the selected `AiderTask`s from a polyglot checkout, in `select` order."""
    return [load_aider_task(checkout, exercise_id) for exercise_id in select]
