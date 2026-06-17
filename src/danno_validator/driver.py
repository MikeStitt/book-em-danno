"""Headless primitives for driving the sandboxed agent-under-test (AUT).

`docker sandbox` publishes no port and mounts no volume, so the portable way to
drive the AUT is captured `exec` of `opencode run --format json` (stdout read on
the host; side effects land in the mounted workspace). The primitives:

- `capture_exec` — the captured counterpart of `book_em_danno`'s
  `exec_in_container` (`bash -lc`, no `-it`).
- `opencode_run` — one headless `opencode run --format json` turn, optionally
  continuing a session (`-s`) for multi-turn Level-0 scripts and selecting an
  agent (`--agent`).
- `reset_workspace` — `git clean -fdx && git reset --hard` between battery runs,
  **guarded** so it can only ever touch a validator-seeded workspace.

Everything routes through `Runner.capture`, so the exact `docker sandbox …`
commands are inspectable and unit-testable without a daemon.

The `--format json` payload schema was pinned against opencode 1.17.7 live in the
sandbox (M1): stdout is **JSONL** — one JSON object per line — interleaved with
the occasional human-readable log block, so it is parsed line-by-line and
non-JSON lines are dropped. Each event is ``{type, timestamp, sessionID, part}``;
the fields the oracle reads are documented on `OpencodeTurn` below.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from book_em_danno.core.exec import CaptureResult, CommandFailedError, Runner

# Dropped into every validator-owned workspace; the gate that lets reset_workspace
# run its destructive git clean/reset (see reset_workspace). git clean excludes it
# so it survives a reset and the guard keeps holding across repeated runs.
WORKSPACE_MARKER = ".danno-validator-workspace"

# Validator work-dir + report root, relative to the invoking cwd (gitignored).
DEFAULT_WORK_DIR = Path(".danno-validator")

# opencode's session-continuation flag. CONFIRMED against opencode 1.17.7 live
# (M1, 2026-06-17): `opencode run --help` lists `-s, --session <id>`. The short
# `-s` form exists too; we use the long form for readability in captured commands.
OPENCODE_SESSION_FLAG = "--session"

# opencode's structured-output flag. The plan said `-f json`, but `-f` is
# `--file` (attach a file) in 1.17.7 — the JSON-events flag is `--format json`
# (M1 live finding). Using `-f json` would silently attach a file named "json".
OPENCODE_FORMAT_FLAG = "--format"


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
    """One captured `opencode run --format json` turn, with the JSONL events parsed.

    `events` is the leniently-parsed JSONL (non-JSON log lines dropped); `raw`
    keeps the unparsed stdout for the reporter. The properties read the schema
    pinned against opencode 1.17.7:

    - each event is ``{type, timestamp, sessionID, part}``;
    - a **text** event (``type == "text"``) carries the assistant text at
      ``part.text``;
    - a **tool** event (``type == "tool"``) carries ``part.tool`` (name),
      ``part.callID`` and ``part.state.status`` ("completed" / "error" / …);
    - a **step_finish** event carries ``part.reason`` (finish reason, e.g.
      "stop"), ``part.tokens`` and ``part.cost``;
    - an **error** event (``type == "error"``) carries the failure under
      ``part``/``error`` (e.g. ``_tag: ProviderModelNotFoundError``).

    These are exactly the signals the Level-0 stall oracle composes with a
    workspace side-effect probe (see `oracle.py`).
    """

    result: CaptureResult
    events: list[dict] = field(default_factory=list)
    raw: str = ""

    @property
    def ok(self) -> bool:
        """True iff the turn ran cleanly: zero exit, at least one parsed event,
        and no error event. (A stalled-but-clean turn is still `ok` — the stall is
        the oracle's call, not a transport failure.)"""
        return self.result.ok and bool(self.events) and not self.errors

    @property
    def session_id(self) -> str | None:
        """The opencode session id (top-level ``sessionID``), for `-s` continuation."""
        for event in self.events:
            sid = event.get("sessionID")
            if sid:
                return str(sid)
        return None

    @property
    def assistant_text(self) -> str:
        """Concatenated assistant text across all ``text`` events (newline-joined)."""
        chunks = [
            text
            for event in self.events
            if event.get("type") == "text"
            and (text := str(event.get("part", {}).get("text", "")).strip())
        ]
        return "\n".join(chunks)

    @property
    def tool_calls(self) -> list[dict]:
        """The ``part`` of every tool event — one per tool call the AUT made.

        opencode emits a tool invocation as either a ``tool`` event (the completed
        part, with ``state.status``) or a ``tool_use`` event (the streamed call),
        and sometimes both for one call — so we collect both types and dedupe by
        ``callID`` (falling back to part ``id``) to count each call once.
        """
        seen: set[str] = set()
        calls: list[dict] = []
        for event in self.events:
            if event.get("type") not in ("tool", "tool_use"):
                continue
            part = event.get("part")
            if not isinstance(part, dict):
                continue
            key = str(part.get("callID") or part.get("id") or id(part))
            if key in seen:
                continue
            seen.add(key)
            calls.append(part)
        return calls

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def finish_reason(self) -> str | None:
        """The ``part.reason`` of the last ``step_finish`` event (e.g. "stop")."""
        reason = None
        for event in self.events:
            if event.get("type") == "step_finish":
                reason = event.get("part", {}).get("reason", reason)
        return str(reason) if reason is not None else None

    @property
    def tokens(self) -> int:
        """Total tokens summed over every step's ``part.tokens.total``."""
        return sum(
            int(event.get("part", {}).get("tokens", {}).get("total", 0) or 0)
            for event in self.events
            if event.get("type") == "step_finish"
        )

    @property
    def cost(self) -> float:
        """Total cost summed over every step's ``part.cost`` (0 for local models)."""
        return sum(
            float(event.get("part", {}).get("cost", 0) or 0)
            for event in self.events
            if event.get("type") == "step_finish"
        )

    @property
    def errors(self) -> list[dict]:
        """Every ``error`` event (provider/model failures, transport errors)."""
        return [event for event in self.events if event.get("type") == "error"]

    @property
    def error_summary(self) -> str | None:
        """A human-readable summary of the first error event, or None.

        opencode error events vary: a `ProviderModelNotFoundError` carries
        `error._tag`, while an upstream `APIError` carries `error.name` and the
        useful detail at `error.data.message` (e.g. "… does not support tools").
        Prefer the most specific available."""
        if not self.errors:
            return None
        err = self.errors[0].get("error", {})
        if not isinstance(err, dict):
            return str(err)
        raw_data = err.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        message = data.get("message")
        tag = err.get("_tag") or err.get("name")
        if tag and message:
            return f"{tag}: {message}"
        return str(tag or message or "error event")


def opencode_run(
    runner: Runner,
    name: str,
    prompt: str,
    *,
    session: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    skip_permissions: bool = False,
    workspace: str | Path | None = None,
) -> OpencodeTurn:
    """Drive one headless `opencode run --format json` turn in sandbox `name`.

    - `session` continues an existing opencode session (multi-turn Level-0 scripts);
    - `agent` selects the opencode agent (the default `run` agent is read-only and
      refuses edits — tool/file tasks need e.g. `--agent build`);
    - `model` overrides the configured model (`-m provider/model`, e.g.
      `ollama/gemma3:27b`) so one sandbox can be swept across models;
    - `skip_permissions` passes `--dangerously-skip-permissions` so a headless turn
      runs autonomously instead of blocking on a permission prompt;
    - `workspace` sets the in-VM exec cwd (`-w`). Note opencode resolves file paths
      against its discovered project root (git/`.opencode` dir), **not** this cwd —
      true workspace isolation comes from mounting the sandbox at the workspace.

    Returns the parsed events alongside the raw capture — never raises on a
    non-zero AUT exit, since a stalled/errored agent turn is the signal the battery
    is measuring.
    """
    cmd = ["docker", "sandbox", "exec"]
    if workspace is not None:
        cmd += ["-w", str(workspace)]
    cmd += [name, "opencode", "run", OPENCODE_FORMAT_FLAG, "json"]
    if agent is not None:
        cmd += ["--agent", agent]
    if model is not None:
        cmd += ["-m", model]
    if skip_permissions:
        cmd.append("--dangerously-skip-permissions")
    if session is not None:
        cmd += [OPENCODE_SESSION_FLAG, session]
    cmd.append(prompt)
    result = runner.capture(cmd)
    return OpencodeTurn(result=result, events=parse_events(result.stdout), raw=result.stdout)


def parse_events(text: str) -> list[dict]:
    """Parse `opencode --format json` stdout (JSONL) into a list of event dicts.

    Lenient by design: `--format json` interleaves the JSONL event stream with the
    occasional human-readable log block (e.g. a multi-line ``[time] ERROR …``
    dump), so any line that is not a single JSON object is dropped rather than
    failing the whole turn. The error is still captured because opencode also
    emits it as a one-line ``{"type":"error",…}`` event.
    """
    events: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


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
