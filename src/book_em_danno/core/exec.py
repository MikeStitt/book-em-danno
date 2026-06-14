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
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

console = Console()


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
    ) -> list[str]:
        """Print and ALWAYS execute, regardless of `apply`. Returns `cmd`.

        For terminal/interactive actions that are the command's whole purpose (the
        `docker sandbox exec -it … <agent>` launch, an interactive shell) rather
        than gated side effects — gating those behind `--apply` is nonsensical, so
        they run unconditionally. Same `CommandFailedError` wrap as `advise`.
        """
        log_info(why)
        console.print(f"  $ {shlex.join(cmd)}")
        self._exec(cmd, cwd=cwd, env=env)
        return cmd

    def _exec(self, cmd: list[str], *, cwd: Path | None, env: dict[str, str] | None) -> None:
        log_debug(f"executing: {shlex.join(cmd)}", verbose=self.verbose)
        try:
            subprocess.run(cmd, check=True, cwd=cwd, env=env)
        except subprocess.CalledProcessError as exc:
            raise CommandFailedError(
                f"command failed (exit {exc.returncode}): {shlex.join(cmd)}"
            ) from exc
