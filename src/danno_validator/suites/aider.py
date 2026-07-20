"""Aider Polyglot suite: self-contained Exercism exercises as `BenchTask`s.

Each exercise (`<lang>/exercises/practice/<slug>/`) ships a stub solution file, a
test file, the language's build scaffolding (`go.mod`, `Cargo.toml`, `CMakeLists.txt`,
`build.gradle` + the gradle wrapper, `package.json`, …), and instructions — the
Exercism layout Aider Polyglot uses. `.meta/config.json` names which files are the
editable `solution`, the grading `test`, and read-only `editor` helpers.

An `AiderTask` seeds the **whole exercise directory** (minus the reference-solution /
docs metadata) into a per-exercise, language-namespaced workspace subdir, prompts the
agent with the instructions, and grades by building + running the exercise's own tests
in the VM. Before grading it restores the canonical test/editor files, so an agent that
edited a test to force a pass is neutralised (Aider's integrity methodology). Exercises
are self-contained (no heavy per-exercise deps), so the default isolation is a shared
sandbox with a per-variant pristine re-seed of the exercise.

`LANG_SPECS` carries, per language, the one-time toolchain install, the test command,
a presence probe, and the toolchain-distribution egress hosts. `select` ids are
`"<lang>/<slug>"` (e.g. `"python/anagram"`).
"""

from __future__ import annotations

import json
import shlex
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from book_em_danno.core.exec import Runner
from danno_validator.driver import capture_exec
from danno_validator.suites.base import GradeResult, grade_report

# Exercise metadata never seeded into the workspace: `.meta` and `.approaches` carry the
# reference solution (a leak), `.docs` duplicates the prompt, `.git` is VCS noise.
# Everything else (stub, tests, editor helpers, build scaffolding) IS seeded.
_SEED_IGNORE = shutil.ignore_patterns(".meta", ".approaches", ".docs", ".git")

# Per-language stamp so a shared sandbox installs each toolchain once (idempotent re-runs).
_TOOLCHAIN_STAMP = "/tmp/.danno-aider-lang-{lang}.ok"

# The `shell` sandbox runs as the unprivileged `agent` user (uid 1000) with passwordless
# sudo; installs that write to system paths (/usr/local, apt, system site-packages) must
# elevate. `$s` resolves to `sudo`, or empty when already root (so the scripts stay
# root-safe too). Prepend to any install command that writes outside $HOME.
_ELEVATE = 'export s=; [ "$(id -u)" != 0 ] && s=sudo; '


@dataclass(frozen=True)
class LangSpec:
    """How to install a language's test toolchain, probe it, and run an exercise's tests."""

    # One-time in-VM toolchain install commands (empty when the base `shell` image has it).
    install: tuple[str, ...]
    # Given the config `test` file relpaths -> the shell command that builds+runs the tests
    # (run with cwd = the exercise dir). Exit 0 iff the tests pass.
    test_command: Callable[[tuple[str, ...]], str]
    # A cheap presence probe (exit 0 iff the toolchain is installed) for the doctor.
    doctor: str
    # Extra egress hosts this toolchain needs on top of the `balanced` base policy — the
    # toolchain-distribution / package hosts the curated base may not already permit.
    egress: tuple[str, ...] = ()


# Arch-portable Go toolchain install: fetch the official tarball for the VM's architecture
# and symlink onto PATH (a login-shell `bash -lc` won't otherwise see /usr/local/go/bin).
_GO_INSTALL = (
    "set -e; " + _ELEVATE + "ver=1.23.4; "
    "arch=$(dpkg --print-architecture 2>/dev/null || uname -m); "
    "case $arch in amd64|x86_64) g=amd64;; arm64|aarch64) g=arm64;; *) g=amd64;; esac; "
    "curl -fsSL https://go.dev/dl/go${ver}.linux-${g}.tar.gz | $s tar -C /usr/local -xz; "
    "$s ln -sf /usr/local/go/bin/go /usr/local/bin/go; "
    "$s ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt"
)

# Rust via rustup (minimal profile) into the agent's $HOME/.cargo, then symlinked onto the
# system PATH so a login-shell `bash -lc` (and grading as the agent user) finds cargo/rustc.
_RUST_INSTALL = (
    "set -e; " + _ELEVATE + "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs "
    "| sh -s -- -y --default-toolchain stable --profile minimal; "
    '$s ln -sf "$HOME/.cargo/bin/cargo" /usr/local/bin/cargo; '
    '$s ln -sf "$HOME/.cargo/bin/rustc" /usr/local/bin/rustc'
)

# JDK 21 (LTS) from Amazon Corretto's stable "latest" tarball. apt on the base image only
# offers JDK 25, but the exercises pin Gradle 8.7, which rejects Java >22 ("Unsupported class
# file major version 69"). Extract to /usr/local, expose a stable JAVA_HOME at /usr/local/jdk21,
# and symlink `java` onto PATH for the presence probe. Mirrors the Go tarball approach.
_JAVA_INSTALL = (
    "set -e; " + _ELEVATE + "arch=$(dpkg --print-architecture 2>/dev/null || uname -m); "
    "case $arch in amd64|x86_64) a=x64;; arm64|aarch64) a=aarch64;; *) a=x64;; esac; "
    "curl -fsSL https://corretto.aws/downloads/latest/amazon-corretto-21-${a}-linux-jdk.tar.gz "
    "| $s tar -C /usr/local -xz; "
    "$s ln -sfn /usr/local/amazon-corretto-21* /usr/local/jdk21; "
    "$s ln -sf /usr/local/jdk21/bin/java /usr/local/bin/java"
)


# Per-language runtime. Python is the historically-verified lane; go/rust/cpp/java/js each
# install their toolchain into the `shell` sandbox on first use (see `install_toolchains`).
LANG_SPECS: dict[str, LangSpec] = {
    "python": LangSpec(
        install=(
            _ELEVATE + "$s python3 -m pip install --break-system-packages --no-cache-dir pytest",
        ),
        test_command=lambda tests: "python3 -m pytest -x -q " + " ".join(map(shlex.quote, tests)),
        doctor="python3 -m pytest --version",
        egress=("pypi.org", "files.pythonhosted.org"),
    ),
    "go": LangSpec(
        install=(_GO_INSTALL,),
        test_command=lambda _tests: "go test ./...",
        doctor="go version",
        egress=("go.dev", "dl.google.com", "storage.googleapis.com", "proxy.golang.org"),
    ),
    "rust": LangSpec(
        install=(_RUST_INSTALL,),
        test_command=lambda _tests: "cargo test",
        doctor="cargo --version",
        egress=("sh.rustup.rs", "static.rust-lang.org", "index.crates.io", "static.crates.io"),
    ),
    "cpp": LangSpec(
        # Catch2 is vendored per-exercise (test/catch.hpp), so only cmake + a compiler are
        # needed. The exercise CMakeLists runs the test binary as part of the build, so a
        # green build == passing tests; a compile error or test failure -> non-zero exit.
        install=(
            _ELEVATE
            + "$s apt-get update && $s apt-get install -y --no-install-recommends cmake g++ make",
        ),
        test_command=lambda _tests: (
            "rm -rf build && cmake -S . -B build >/dev/null && cmake --build build"
        ),
        doctor="cmake --version && g++ --version",
        egress=(
            "deb.debian.org",
            "security.debian.org",
            "archive.ubuntu.com",
            "security.ubuntu.com",
        ),
    ),
    "java": LangSpec(
        install=(_JAVA_INSTALL,),
        # The gradle wrapper downloads gradle itself; junit/assertj come from Maven Central.
        # Pin JAVA_HOME to the JDK 21 tarball so gradle uses it regardless of PATH/base image.
        test_command=lambda _tests: (
            "export JAVA_HOME=/usr/local/jdk21; "
            'export PATH="$JAVA_HOME/bin:$PATH"; '
            "./gradlew test --no-daemon --console=plain"
        ),
        doctor="java -version",
        egress=(
            "corretto.aws",
            "services.gradle.org",
            "repo.maven.apache.org",
            "repo1.maven.org",
            "plugins.gradle.org",
        ),
    ),
    "javascript": LangSpec(
        # node + npm are in the base image; jest/babel install per-exercise from package.json.
        install=(),
        test_command=lambda _tests: "npm install --no-audit --no-fund --silent && npm test",
        doctor="node --version && npm --version",
        egress=("registry.npmjs.org",),
    ),
}


def languages_in(select: Sequence[str]) -> list[str]:
    """The distinct languages named in a `select` list, in first-seen order."""
    seen: list[str] = []
    for exercise_id in select:
        lang = exercise_id.split("/", 1)[0]
        if lang and lang not in seen:
            seen.append(lang)
    return seen


def toolchain_egress(languages: Sequence[str]) -> tuple[str, ...]:
    """The union of the toolchain-distribution egress hosts the given languages need
    (on top of the `balanced` base policy), de-duplicated in first-seen order."""
    hosts: list[str] = []
    for lang in languages:
        spec = LANG_SPECS.get(lang)
        if spec is None:
            continue
        for host in spec.egress:
            if host not in hosts:
                hosts.append(host)
    return tuple(hosts)


def install_toolchains(runner: Runner, sandbox: str, languages: Sequence[str]) -> None:
    """One-time (stamp-guarded) install of each language's test toolchain in `sandbox`.

    Called once per sandbox after the harness is installed and egress is armed. Each
    language's install is skipped if its stamp exists, so re-running across the model
    matrix is cheap. Fails loud (Working Rule 8) if a selected toolchain won't install.
    """
    for lang in languages:
        spec = LANG_SPECS.get(lang)
        if spec is None or not spec.install:
            continue
        stamp = shlex.quote(_TOOLCHAIN_STAMP.format(lang=lang))
        script = " && ".join(spec.install)
        guarded = f"test -f {stamp} && exit 0; ( {script} ) && touch {stamp}"
        capture_exec(runner, sandbox, guarded, check=True)


def doctor_toolchains(runner: Runner, sandbox: str, languages: Sequence[str]) -> dict[str, bool]:
    """Probe each language's toolchain in `sandbox`; `{lang: present?}`. An unknown
    language (no `LangSpec`) reads as absent."""
    result: dict[str, bool] = {}
    for lang in languages:
        spec = LANG_SPECS.get(lang)
        if spec is None:
            result[lang] = False
            continue
        result[lang] = capture_exec(runner, sandbox, spec.doctor, check=False).ok
    return result


@dataclass(frozen=True)
class AiderTask:
    """One Aider Polyglot exercise mapped onto the `BenchTask` contract.

    Seeded into a per-exercise, language-namespaced subdir of the mounted workspace so
    same-slug exercises across languages never collide. `provision` copies the whole
    exercise dir (minus the reference-solution / docs metadata) so the build scaffolding
    is present; `reset` restores the editable stub(s) before the first attempt; `grade`
    restores the canonical test/editor files (integrity) and builds+runs the tests.
    """

    exercise_id: str  # "python/anagram"
    language: str
    instructions: str
    root: Path  # the checkout exercise dir — the source the workspace is seeded from
    solution_files: tuple[tuple[str, str], ...]  # (relpath, original stub) — editable
    protected_files: tuple[tuple[str, str], ...]  # (relpath, canonical) — tests + editor helpers
    test_files: tuple[str, ...]  # config `test` relpaths — the grade target
    subdir: str  # workspace-relative dir the exercise is seeded into ("<lang>/<slug>")

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
        """Seed a pristine copy of the whole exercise (minus reference-solution/docs
        metadata) into the per-exercise subdir, wiping any prior variant's edits and
        build artifacts first. The language toolchain is installed once per sandbox by
        `install_toolchains`, not here."""
        d = self._dir(workspace)
        if d.exists():
            shutil.rmtree(d)
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.root, d, ignore=_SEED_IGNORE)

    def reset(self, runner: Runner, sandbox: str, workspace: Path) -> None:
        """Restore the editable stub(s) before the first attempt (keep tests + scaffolding)."""
        d = self._dir(workspace)
        for relpath, content in self.solution_files:
            (d / relpath).write_text(content, encoding="utf-8")

    def grade(self, runner: Runner, sandbox: str, workspace: Path) -> GradeResult:
        """Restore the canonical test/editor files (so a tampered test cannot force a
        pass), then build + run the exercise's tests in the seeded subdir."""
        d = self._dir(workspace)
        for relpath, content in self.protected_files:
            target = d / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        spec = LANG_SPECS[self.language]
        cmd = f"cd {shlex.quote(str(d))} && {spec.test_command(self.test_files)}"
        res = capture_exec(runner, sandbox, cmd, check=False)
        return GradeResult(passed=res.ok, report=grade_report(res.stdout, res.stderr))

    def workspace_dir(self, workspace: Path) -> Path:
        """The in-VM cwd a turn should use for this exercise (its seeded subdir)."""
        return self._dir(workspace)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_aider_task(checkout: Path, exercise_id: str) -> AiderTask:
    """Build an `AiderTask` from a polyglot-benchmark checkout for `<lang>/<slug>`.

    Reads `.meta/config.json` (the solution / test / editor file split), the stub
    solution file(s), the test + editor file(s), and the instructions. Fails loud
    (ValueError) on an unknown language, a missing exercise, or a malformed config.
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
    editor = files.get("editor") or []
    if not solution or not test:
        raise ValueError(f"aider: {exercise_id} config lists no solution/test files")
    instructions_path = root / ".docs" / "instructions.md"
    instructions = _read(instructions_path) if instructions_path.is_file() else slug
    append = root / ".docs" / "instructions.append.md"
    if append.is_file():
        instructions += "\n\n" + _read(append)
    # Protected = the grading tests plus read-only editor helpers (e.g. go's table-driven
    # `cases_test.go`) — both restored from canonical before grading so agent edits to them
    # cannot skew the result. Skip any listed file that isn't present on disk.
    protected = tuple(
        (rel, _read(root / rel)) for rel in (*test, *editor) if (root / rel).is_file()
    )
    return AiderTask(
        exercise_id=exercise_id,
        language=language,
        instructions=instructions,
        root=root,
        solution_files=tuple((rel, _read(root / rel)) for rel in solution),
        protected_files=protected,
        test_files=tuple(test),
        subdir=f"{language}/{slug}",
    )


def load_aider_tasks(checkout: Path, select: list[str]) -> list[AiderTask]:
    """Build the selected `AiderTask`s from a polyglot checkout, in `select` order."""
    return [load_aider_task(checkout, exercise_id) for exercise_id in select]
