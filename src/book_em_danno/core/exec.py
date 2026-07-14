"""Logging + the two-mode command Runner.

Ports `scripts/lib/common.sh`. The defining behavioral shift from the Bash
original: Bash `run_cmd` *executes* by default. Ours **advises** by default — it
prints the literal copy-paste command and runs it only under `--apply`. This is
the non-destructive/idempotent install rule applied to every host/Docker/Ollama
side effect: the human sees exactly what would run and opts in.

`advise()` returns the command list so tests can assert exact construction
without a Docker daemon or Ollama server (Working Rule 7: I/O in a thin, mockable
wrapper; core logic stays inspectable).
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from rich.console import Console

console = Console()

# How often the runaway-gate watchdog polls the tally + wall clock while a harness cell
# runs. Fine enough to bound overshoot to a few calls/seconds, coarse enough to be free.
_WATCH_INTERVAL_S = 0.25


def log_info(msg: str) -> None:
    console.print(f"[INFO] {msg}")


def log_warn(msg: str) -> None:
    console.print(f"[yellow][WARN][/yellow] {msg}")


def log_err(msg: str) -> None:
    console.print(f"[red][ERROR][/red] {msg}")


def log_debug(msg: str, *, verbose: bool) -> None:
    if verbose:
        console.print(f"[dim][DEBUG] {msg}[/dim]")


class CommandNotFoundError(Exception):
    """A required external command is not on PATH (fail loud, Working Rule 8)."""


class CommandFailedError(Exception):
    """An advised command was executed under --apply and exited non-zero."""


@dataclass
class CaptureResult:
    """Outcome of `Runner.capture`: the exact command plus its captured streams."""

    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class GateProbe(Protocol):
    """Live per-cell counters the runaway-gate watchdog polls. Satisfied structurally by
    `book_em_danno.capture.gate.GateTally` (no import — keeps `core` below `capture`)."""

    def inference_calls(self) -> int: ...

    def tokens(self) -> int: ...


@dataclass(frozen=True)
class GateBreach:
    """Which runaway gate tripped, and the observed value vs its limit, when the watchdog
    killed a cell. `gate` is the verdict slug: `runaway` | `over-budget` | `timeout`."""

    gate: str
    observed: float
    limit: float


@dataclass
class GateWatch:
    """A `danno bench` cell's runaway-gate limits + outcome. `Runner.watching()` installs
    one for the duration of a harness turn; the watched `capture()` sets `breach` if it had
    to kill the process. A `None` limit disables that gate. See
    `.docs/plan-bench-runaway-gates.md`."""

    probe: GateProbe | None = None
    max_turns: int | None = None  # Gate 1 — inference calls
    max_tokens: int | None = None  # Gate 2 — total tokens
    timeout_s: float | None = None  # Gate 3 — wall-clock backstop
    breach: GateBreach | None = None

    def check(self, elapsed: float) -> GateBreach | None:
        """The first gate that has tripped at `elapsed` seconds, or None. Gate 1/2 need a
        live `probe`; Gate 3 (wall clock) always applies."""
        if self.probe is not None and self.max_turns is not None:
            calls = self.probe.inference_calls()
            if calls > self.max_turns:
                return GateBreach("runaway", calls, self.max_turns)
        if self.probe is not None and self.max_tokens is not None:
            toks = self.probe.tokens()
            if toks > self.max_tokens:
                return GateBreach("over-budget", toks, self.max_tokens)
        if self.timeout_s is not None and elapsed > self.timeout_s:
            return GateBreach("timeout", round(elapsed, 1), self.timeout_s)
        return None


def require_cmd(name: str, *, fix: str | None = None) -> str:
    """Return the resolved path to `name`, or fail loud with a fix hint."""
    path = shutil.which(name)
    if path is None:
        hint = f" — {fix}" if fix else ""
        raise CommandNotFoundError(f"required command not found: {name}{hint}")
    return path


@dataclass
class Runner:
    """Executes host/Docker/Ollama commands under the two-mode policy.

    - default (`apply=False`): print the copy-paste command, run nothing — the
      user runs it themselves.
    - `--apply`: print and execute via `subprocess.run`.
    """

    apply: bool = False
    verbose: bool = False
    # When set (via `watching()`), the next `capture()` is wrapped by the runaway-gate
    # watchdog. Kept on the Runner so the turn drivers' `capture()` calls stay unchanged —
    # bench installs the watch around the single HUT turn exec.
    _watch: GateWatch | None = field(default=None, repr=False, compare=False)

    @contextmanager
    def watching(
        self,
        *,
        probe: GateProbe | None = None,
        max_turns: int | None = None,
        max_tokens: int | None = None,
        timeout_s: float | None = None,
    ) -> Iterator[GateWatch]:
        """Wrap `capture()` calls in this block with the runaway-gate watchdog. Yields the
        `GateWatch` so the caller can read `.breach` after the turn. Nesting restores the
        previous watch on exit (there is only ever one active HUT turn)."""
        watch = GateWatch(
            probe=probe, max_turns=max_turns, max_tokens=max_tokens, timeout_s=timeout_s
        )
        prev = self._watch
        self._watch = watch
        try:
            yield watch
        finally:
            self._watch = prev

    def advise(
        self,
        cmd: list[str],
        why: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Advise (and under --apply, run) a single command. Returns `cmd`.

        `cwd`/`env` apply only when executing under --apply: the printed
        copy-paste line stays the bare command (host cwd/env aren't part of it —
        document them in `why=` as the ADOS installer does). `env=None` keeps the
        inherited environment; callers that set it pass `{**os.environ, …}`.

        A non-zero exit under --apply raises `CommandFailedError` (a clean,
        CLI-catchable error) rather than letting `CalledProcessError` surface as a
        traceback.
        """
        log_info(why)
        console.print(f"  $ {shlex.join(cmd)}")
        if self.apply:
            self._exec(cmd, cwd=cwd, env=env)
        return cmd

    def run(
        self,
        cmd: list[str],
        why: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> list[str]:
        """Print and ALWAYS execute, regardless of `apply`. Returns `cmd`.

        For terminal/interactive actions that are the command's whole purpose (the
        `docker sandbox exec -it … <agent>` launch, an interactive shell) rather
        than gated side effects — gating those behind `--apply` is nonsensical, so
        they run unconditionally.

        `check=True` wraps a non-zero exit in `CommandFailedError` like `advise`.
        Pass `check=False` for an interactive TUI/shell: once we have handed the
        terminal to the agent, its exit code reflects the user's session (quitting,
        declining a prompt), not a danno provisioning failure, so a non-zero exit is
        not escalated to an error (it is logged at debug under --verbose).
        """
        log_info(why)
        console.print(f"  $ {shlex.join(cmd)}")
        self._exec(cmd, cwd=cwd, env=env, check=check)
        return cmd

    def capture(
        self,
        cmd: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = False,
    ) -> CaptureResult:
        """Execute `cmd`, capture stdout/stderr, and return a `CaptureResult`.

        Unlike `advise`/`run` this is for *reading state the harness must inspect*
        (a captured `opencode run -f json` turn, a workspace probe): it always
        executes (apply-independent, like `run`) but does NOT stream — output is
        captured for the caller to parse.

        `check=False` by default: a non-zero exit from the harness-under-test is
        data to inspect, not a danno failure. Pass `check=True` to raise
        `CommandFailedError` on a non-zero exit (e.g. a workspace reset that must
        succeed). The command is logged only under `--verbose` (machine-driven; no
        copy-paste line).
        """
        log_debug(f"capturing: {shlex.join(cmd)}", verbose=self.verbose)
        if self._watch is not None:
            return self._capture_watched(cmd, cwd=cwd, env=env, check=check, watch=self._watch)
        result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise CommandFailedError(
                f"command failed (exit {result.returncode}): {shlex.join(cmd)}"
            )
        return CaptureResult(cmd, result.returncode, result.stdout, result.stderr)

    def _capture_watched(
        self,
        cmd: list[str],
        *,
        cwd: Path | None,
        env: dict[str, str] | None,
        check: bool,
        watch: GateWatch,
    ) -> CaptureResult:
        """`capture()` under the runaway-gate watchdog: spawn via `Popen`, drain stdout/
        stderr on reader threads (so a chatty child never deadlocks on a full pipe), and
        poll `watch.check()` every `_WATCH_INTERVAL_S`. On the first breach, kill the
        process and record it on `watch.breach`; the partial output is still returned."""
        proc = subprocess.Popen(  # noqa: S603 - cmd is built from trusted internal argv
            cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        out: list[str] = []
        err: list[str] = []
        readers = [
            threading.Thread(target=lambda: out.append(proc.stdout.read() if proc.stdout else "")),
            threading.Thread(target=lambda: err.append(proc.stderr.read() if proc.stderr else "")),
        ]
        for reader in readers:
            reader.start()
        start = time.monotonic()
        while True:
            try:
                proc.wait(timeout=_WATCH_INTERVAL_S)
                break  # child exited on its own
            except subprocess.TimeoutExpired:
                breach = watch.check(time.monotonic() - start)
                if breach is not None:
                    watch.breach = breach
                    proc.kill()
                    proc.wait()
                    break
        for reader in readers:
            reader.join()
        result = CaptureResult(cmd, proc.returncode, out[0] if out else "", err[0] if err else "")
        # A gate kill is an expected outcome (the caller reads `watch.breach`), never a
        # CommandFailedError; only a genuine non-gate non-zero exit escalates under `check`.
        if check and result.returncode != 0 and watch.breach is None:
            raise CommandFailedError(
                f"command failed (exit {result.returncode}): {shlex.join(cmd)}"
            )
        return result

    def _exec(
        self, cmd: list[str], *, cwd: Path | None, env: dict[str, str] | None, check: bool = True
    ) -> None:
        log_debug(f"executing: {shlex.join(cmd)}", verbose=self.verbose)
        if not check:
            result = subprocess.run(cmd, cwd=cwd, env=env)
            if result.returncode != 0:
                log_debug(
                    f"interactive session exited with status {result.returncode}",
                    verbose=self.verbose,
                )
            return
        try:
            subprocess.run(cmd, check=True, cwd=cwd, env=env)
        except subprocess.CalledProcessError as exc:
            raise CommandFailedError(
                f"command failed (exit {exc.returncode}): {shlex.join(cmd)}"
            ) from exc
