"""M0 headless primitives for driving the sandboxed agent-under-test (AUT).

`docker sandbox` publishes no port and mounts no volume, so the portable way to
drive the AUT is captured `exec` of `opencode run -f json` (stdout read on the
host; side effects land in the mounted workspace). These are the only three
primitives M0 needs:

- `capture_exec` — the captured counterpart of `book_em_danno`'s
  `exec_in_container` (`bash -lc`, no `-it`).
- `opencode_run` — one headless `opencode run -f json` turn, optionally continuing
  a session for multi-turn Level-0 scripts.
- `reset_workspace` — `git clean -fdx && git reset --hard` between battery runs,
  **guarded** so it can only ever touch a validator-seeded workspace.

Everything routes through `Runner.capture`, so the exact `docker sandbox …`
commands are inspectable and unit-testable without a daemon.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

from book_em_danno.core.exec import CaptureResult, CommandFailedError, Runner

# Dropped into every validator-owned workspace; the gate that lets reset_workspace
# run its destructive git clean/reset (see reset_workspace). git clean excludes it
# so it survives a reset and the guard keeps holding across repeated runs.
WORKSPACE_MARKER = ".danno-validator-workspace"

# Validator work-dir + report root, relative to the invoking cwd (gitignored).
DEFAULT_WORK_DIR = Path(".danno-validator")

# opencode's session-continuation flag, taken from the validator plan — NOT from
# running opencode (the host invariant forbids that). Confirm it against the
# installed opencode version when M1 first drives a live turn.
OPENCODE_SESSION_FLAG = "--session"


def capture_exec(runner: Runner, name: str, command: str, *, check: bool = False) -> CaptureResult:
    """Run a shell command inside sandbox `name`, captured (non-tty `bash -lc`).

    The captured counterpart of `book_em_danno.commands.sandbox.exec_in_container`:
    same `bash -lc` shape, but stdout/stderr/exit are returned for the harness to
    inspect rather than streamed. `exec` auto-starts a stopped VM, so no explicit
    start is needed.
    """
    return runner.capture(["docker", "sandbox", "exec", name, "bash", "-lc", command], check=check)


@dataclass
class OpencodeTurn:
    """One captured `opencode run -f json` turn.

    `payload` is the leniently-parsed `-f json` stdout (None if it didn't parse);
    `raw` keeps the unparsed stdout for the reporter. M0 deliberately does not
    interpret the payload's fields — the schema is pinned down live at M1, where
    the stall oracle starts reading tool-call counts and finish reasons from it.
    """

    result: CaptureResult
    payload: object | None
    raw: str

    @property
    def ok(self) -> bool:
        return self.result.ok and self.payload is not None


def opencode_run(
    runner: Runner,
    name: str,
    prompt: str,
    *,
    session: str | None = None,
    workspace: str | Path | None = None,
) -> OpencodeTurn:
    """Drive one headless `opencode run -f json` turn in sandbox `name`, captured.

    `session` continues an existing opencode session (multi-turn Level-0 scripts);
    `workspace` sets the in-VM working dir (`-w`). Returns the parsed payload (or
    None) alongside the raw capture — never raises on a non-zero AUT exit, since a
    stalled/errored agent turn is the signal the battery is measuring.
    """
    cmd = ["docker", "sandbox", "exec"]
    if workspace is not None:
        cmd += ["-w", str(workspace)]
    cmd += [name, "opencode", "run", "-f", "json"]
    if session is not None:
        cmd += [OPENCODE_SESSION_FLAG, session]
    cmd.append(prompt)
    result = runner.capture(cmd)
    return OpencodeTurn(result=result, payload=_parse_json(result.stdout), raw=result.stdout)


def _parse_json(text: str) -> object | None:
    """Parse `opencode -f json` stdout leniently; None when it isn't valid JSON."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def is_validator_workspace(path: Path) -> bool:
    """True iff `path` carries the validator's ownership marker — the gate that
    keeps the destructive reset from ever touching a non-validator repo."""
    return (path / WORKSPACE_MARKER).is_file()


def seed_workspace(path: Path) -> Path:
    """Create (idempotently) a validator-owned workspace dir and drop the ownership
    marker so `reset_workspace` will operate on it. Returns `path`.

    M0 only marks the dir; git-init + benchmark seeding is the adapter's job (M1+).
    """
    path.mkdir(parents=True, exist_ok=True)
    (path / WORKSPACE_MARKER).touch()
    return path


def reset_workspace(
    runner: Runner, name: str, workspace: Path, *, check: bool = True
) -> CaptureResult:
    """Reset the mounted `workspace` to a clean state between battery runs:
    `git clean -fdx && git reset --hard`, executed in the VM at `workspace`.

    DESTRUCTIVE and **guarded** (Working Rules 6 & 8): refuses with a loud
    `CommandFailedError` unless `workspace` carries the `.danno-validator-workspace`
    marker, so a misconfigured path can never wipe a real repo. `git clean` excludes
    the marker (`-e`) so the guard keeps holding across repeated resets.
    """
    if not is_validator_workspace(workspace):
        raise CommandFailedError(
            f"refusing to reset {workspace}: missing the {WORKSPACE_MARKER} marker. "
            "reset_workspace only operates on validator-seeded workspaces — call "
            "seed_workspace() first."
        )
    command = (
        f"cd {shlex.quote(str(workspace))} && "
        f"git clean -fdx -e {shlex.quote(WORKSPACE_MARKER)} && git reset --hard"
    )
    return capture_exec(runner, name, command, check=check)
