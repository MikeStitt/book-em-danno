"""Headless primitives for driving the sandboxed harness-under-test (HUT).

`docker sandbox` publishes no port and mounts no volume, so the portable way to
drive the HUT is captured `exec` of `opencode run --format json` (stdout read on
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
from typing import Protocol, runtime_checkable

from book_em_danno.commands import sandbox_cli
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

# Claude Code headless flags (M5). PIN LIVE against the installed `claude` version
# before relying on them (M1 set the precedent of confirming agent flags live):
# `-p`/`--print` runs headless; `--output-format stream-json` (with `--verbose`)
# emits the per-message JSONL the parser below reads; `--resume <id>` continues a
# session for the multi-turn Level-0 script; `--dangerously-skip-permissions`
# auto-approves tools so a headless turn runs unattended. Claude ignores opencode's
# `-m`/`--agent`, so the baseline drives its fixed default model/agent.
CLAUDE_PRINT_FLAG = "-p"
CLAUDE_FORMAT_FLAG = "--output-format"
CLAUDE_FORMAT_VALUE = "stream-json"
CLAUDE_RESUME_FLAG = "--resume"
CLAUDE_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
# `--model <alias|full-name>` (e.g. "opus"/"sonnet"/"fable" or "claude-opus-4-8").
# Pinning it is REQUIRED for a meaningful baseline: like opencode's `-m`, the
# model drives cost/latency/behavior, so it must be controlled, not left to the
# install default. CONFIRMED against claude 2.1.179 `claude --help`.
CLAUDE_MODEL_FLAG = "--model"

# Claurst headless flags (M0 spike, claurst 0.1.5, 2026-06-23). Claurst's CLI is
# Claude-Code-faithful: `-p` headless, `-m ollama/<tag>` model, `--output-format
# stream-json` JSONL, `--resume <id>` session, `--dangerously-skip-permissions`
# (alias --yolo) auto-approves, `--cwd <dir>` exec cwd, `--max-turns N`. UNLIKE
# claude, claurst USES `-m` (it has no opencode `--agent`). DO NOT pass `--verbose`
# — it dumps ANSI DEBUG logs onto stdout and breaks JSONL parsing.
CLAURST_PRINT_FLAG = "-p"
CLAURST_FORMAT_FLAG = "--output-format"
CLAURST_FORMAT_VALUE = "stream-json"
CLAURST_MODEL_FLAG = "-m"
CLAURST_RESUME_FLAG = "--resume"
CLAURST_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
CLAURST_CWD_FLAG = "--cwd"

# Relay-free local Ollama (plan W3, verified S1 2026-07-11 on sbx AND legacy): the
# danno fork build's reqwest DOES honor the sandbox egress proxy, so claurst reaches
# host Ollama directly at `host.docker.internal:11434` (rewritten to `localhost` by
# both proxies) — no in-VM relay. (The old "Rust client ignores HTTP(S)_PROXY" note
# was outdated for the fork build.)
CLAURST_RELAY_FREE_OLLAMA_HOST = "http://host.docker.internal:11434"

# The in-VM relay (listens on 127.0.0.1:11434, re-issues THROUGH the egress proxy) is now
# used ONLY by occ-on-legacy / occ-under-capture (see `occ_run` + `OCC_LOCAL_OPENAI_ENV`);
# claurst is fully relay-free (W3 + W6). See `_claurst_script`.


def _claurst_ollama_host(capture_port: int | None) -> str:
    """The relay-free `OLLAMA_HOST` for a LOCAL claurst turn (W3 + W6). Normally host
    Ollama at `host.docker.internal:11434`; under `--capture` the host-side recording
    proxy at `host.docker.internal:<capture_port>` (allowed in egress by
    `capture_allow_hosts`). Both are reached through the sandbox egress proxy — no relay."""
    if capture_port is None:
        return CLAURST_RELAY_FREE_OLLAMA_HOST
    return f"http://host.docker.internal:{capture_port}"


# Under `--capture` the relay forwards to a host-side recording proxy (capture/proxy.py)
# instead of host Ollama directly, so claurst's wire traffic is recorded with the same
# machinery as opencode's. The launcher sets this env var to the proxy's host port; the
# relay reads it and defaults to the real Ollama port when absent (the non-capture case).
CLAURST_RELAY_UPSTREAM_ENV = "DANNO_RELAY_UPSTREAM_PORT"
CLAURST_RELAY_DEFAULT_UPSTREAM_PORT = 11434

# How long the relay waits on a single upstream read before giving up (seconds). Slow
# local models (large context, tiny batch → minutes-long prefill on a non-streaming POST)
# can exceed the old hardcoded 600s, so the relay reads `DANNO_RELAY_TIMEOUT` (default
# 3600 = 60 min; <= 0 disables the timeout). `_claurst_script` sets it on the launch line
# but honors an inherited value first, so `danno.toml [env]`/an exported var can override.
# Shared by claurst + occ (both ride `_claurst_script`); only ever raises a ceiling.
CLAURST_RELAY_TIMEOUT_ENV = "DANNO_RELAY_TIMEOUT"
CLAURST_RELAY_DEFAULT_TIMEOUT = 3600

# occ (open-claude-code) headless flags — pinned against danno's fork
# (`MikeStitt/open-claude-code`, branch `danno-integration`; see `occ.OCC_REF_DEFAULT`).
# occ is a Node/ESM Claude-Code clone run headless as `node <clone>/v2/src/index.mjs`. Its
# CLI mirrors claude's: `-p` prompt, `-m` model, `--output-format stream-json` JSONL,
# `--permission-mode bypassPermissions` auto-approves tools (safe ONLY because the Docker
# sandbox is the isolation boundary — occ's Bash tool has no isolation of its own),
# `--max-turns N` bounds the agent loop. The fork's `detectProvider` natively routes to the
# OpenAI-compatible path whenever OPENAI_BASE_URL is set (no source patch), and its global
# undici dispatcher honors HTTPS_PROXY (no NODE_OPTIONS shim) — so both local Ollama (via
# the relay) and cloud (OpenAI-compatible) work. occ is installed VM-local at a FIXED
# absolute path (not $HOME-relative) so a relocated HOME (agent-home) cannot move the
# entrypoint out from under the driver — mirrors how claurst's binary stays VM-local.
OCC_ENTRY = "/home/agent/.local/share/danno/occ/v2/src/index.mjs"
OCC_PRINT_FLAG = "-p"
OCC_FORMAT_FLAG = "--output-format"
OCC_FORMAT_VALUE = "stream-json"
OCC_MODEL_FLAG = "-m"
OCC_PERMISSION_FLAG = "--permission-mode"
OCC_PERMISSION_VALUE = "bypassPermissions"
OCC_MAX_TURNS_FLAG = "--max-turns"
OCC_DEFAULT_MAX_TURNS = 30
# occ's OpenAI/Ollama path is non-streaming; its default streaming path crashes
# (`Symbol.asyncIterator` on undefined), so every occ invocation forces it off.
OCC_STREAMING_ENV = "CLAUDE_CODE_STREAMING=0"
# On the LOCAL path occ talks to the in-VM relay as an OpenAI-compatible endpoint and
# requires the Bearer header even though Ollama ignores it — a dummy key satisfies it.
OCC_LOCAL_OPENAI_ENV = "OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=dummy"
_OLLAMA_RELAY_SOURCE = r"""
import os, sys, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = "http://host.docker.internal:" + os.environ.get("DANNO_RELAY_UPSTREAM_PORT", "11434")
# Single-upstream-read timeout (seconds). Default 3600 (60 min) so a slow local model's
# minutes-long prefill on a non-streaming POST is not cut off; <= 0 means no timeout.
_TIMEOUT = int(os.environ.get("DANNO_RELAY_TIMEOUT", "3600"))
TIMEOUT = _TIMEOUT if _TIMEOUT > 0 else None
PROXY = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY} if PROXY else {})
)

# Opt-in flushed trace of BOTH ends (claurst<->relay<->upstream), off unless
# DANNO_RELAY_LOG names a writable path. Each line is timestamped + per-connection
# thread-tagged and FLUSHED immediately, so a hang shows up as the last line written
# (a buffered log would swallow it): no "<- upstream" after "-> upstream" => stuck on
# the upstream read; "RESP done"/"CONN close" with no following "REQ" => claurst never
# sent the next request (claurst-side). Diagnostic only; default behaviour unchanged.
_LOG_PATH = os.environ.get("DANNO_RELAY_LOG")
_LOG_LOCK = threading.Lock()


def _log(msg):
    if not _LOG_PATH:
        return
    with _LOG_LOCK, open(_LOG_PATH, "a") as fh:
        fh.write("%.3f c%d %s\n" % (time.time(), threading.get_ident() % 100000, msg))
        fh.flush()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        _log("CONN open %s:%s" % self.client_address)

    def finish(self):
        _log("CONN close")
        super().finish()

    def _f(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(n) if n else None
        _log("REQ %s %s clen=%d keepalive=%s"
             % (self.command, self.path, n, not self.close_connection))
        req = urllib.request.Request(UPSTREAM + self.path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "proxy-connection", "connection"):
                req.add_header(k, v)
        _log("-> upstream %s%s" % (UPSTREAM, self.path))
        try:
            r = _opener.open(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            r = e
        except Exception as e:
            _log("ERR upstream %r" % e)
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())
            return
        _log("<- upstream status=%s ct=%s" % (r.status, r.getheader("Content-Type")))
        self.send_response(r.status)
        for k, v in r.getheaders():
            if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        total = 0
        while True:
            c = r.read(4096)
            if not c:
                break
            total += len(c)
            self.wfile.write(b"%X\r\n" % len(c) + c + b"\r\n")
        self.wfile.write(b"0\r\n\r\n")
        _log("RESP done %dB" % total)

    do_GET = do_POST = do_PUT = do_DELETE = _f

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 11434
    ThreadingHTTPServer(("127.0.0.1", p), H).serve_forever()
"""


@runtime_checkable
class Turn(Protocol):
    """The harness-agnostic read surface a captured turn must expose.

    Both `OpencodeTurn` and `ClaudeTurn` satisfy this structurally (no
    inheritance), so the oracle, the level runners, and the reporter consume
    either transcript format unchanged — the comparison is on harness-agnostic
    signals (text + tool calls + the caller's workspace side-effect probe), not on
    opencode-vs-claude event shapes. These are exactly the members read by
    `oracle.classify_turn`, the `*Result` dataclasses, and `report.py`.
    """

    @property
    def assistant_text(self) -> str: ...
    @property
    def tool_calls(self) -> list[dict]: ...
    @property
    def tool_call_count(self) -> int: ...
    @property
    def session_id(self) -> str | None: ...
    @property
    def tokens(self) -> int: ...
    @property
    def cost(self) -> float: ...
    @property
    def errors(self) -> list[dict]: ...
    @property
    def error_summary(self) -> str | None: ...


class TurnFn(Protocol):
    """The call signature shared by `opencode_run` and `claude_run`.

    The level runners take one of these as their (injectable) turn producer, so
    the same battery drives either agent. The claude adapter accepts `agent`/
    `model` for signature compatibility but ignores them (the baseline is the
    fixed default Claude config).
    """

    def __call__(
        self,
        runner: Runner,
        name: str,
        prompt: str,
        *,
        session: str | None = ...,
        agent: str | None = ...,
        model: str | None = ...,
        skip_permissions: bool = ...,
        workspace: str | Path | None = ...,
    ) -> Turn: ...


def capture_exec(runner: Runner, name: str, command: str, *, check: bool = False) -> CaptureResult:
    """Run a shell command inside sandbox `name`, captured (non-tty `bash -lc`).

    The captured counterpart of `book_em_danno.commands.sandbox.exec_in_container`:
    same `bash -lc` shape, but stdout/stderr/exit are returned for the harness to
    inspect rather than streamed. `exec` auto-starts a stopped VM, so no explicit
    start is needed.
    """
    return runner.capture([*sandbox_cli.base(), "exec", name, "bash", "-lc", command], check=check)


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
        """The ``part`` of every tool event — one per tool call the HUT made.

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
    env_file: str | Path | None = None,
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
    - `env_file` is passed to `docker sandbox exec` as `--env-file` — how cloud
      configs in a sweep get their credentials: a bare exec inherits no host
      environment, so a model whose provider needs e.g. `ANTHROPIC_API_KEY` /
      `NVIDIA_API_KEY` errors at L0 without it. Local Ollama models need none (the
      base URL is baked into `opencode.jsonc`). The sweep builds this chmod-600
      file from `--env`/host-exported keys (see `sweep._authed_opencode_run`).

    Returns the parsed events alongside the raw capture — never raises on a
    non-zero HUT exit, since a stalled/errored agent turn is the signal the battery
    is measuring.
    """
    cmd = [*sandbox_cli.base(), "exec"]
    if workspace is not None:
        cmd += ["-w", str(workspace)]
    if env_file is not None:
        cmd += ["--env-file", str(env_file)]
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


@dataclass
class ClaudeTurn:
    """One captured `claude -p --output-format stream-json` turn, parsed.

    Claude Code's stream-json is JSONL like opencode's, but a different schema, so
    this normalises it onto the same `Turn` read surface (see `Turn`). The schema
    is PINNED LIVE against the installed `claude` version (M1 discipline); the
    fields mapped here, against current Claude Code:

    - a **system** init event (`type == "system"`) carries `session_id`;
    - an **assistant** event (`type == "assistant"`) carries `message.content`, a
      list of blocks — `{"type":"text","text":…}` and
      `{"type":"tool_use","id":…,"name":…,"input":…}`;
    - a **user** event (`type == "user"`) carries `tool_result` blocks with
      `tool_use_id` and `is_error` — used to mark the matching tool call's status;
    - a final **result** event (`type == "result"`) carries `result` (final text),
      `total_cost_usd`, `usage` (token counts), `session_id`, and `is_error` /
      `subtype` (a non-success subtype / `is_error` true ⇒ a failed turn).

    Tool calls are re-shaped to ``{"tool": name, "callID": id, "state":
    {"status": "completed"|"error"}}`` so the *same* oracle branch
    (`state.status == "error"`) and reporter (`c.get("tool")`) work across agents.
    """

    result: CaptureResult
    events: list[dict] = field(default_factory=list)
    raw: str = ""

    @property
    def ok(self) -> bool:
        """True iff the turn ran cleanly: zero exit, at least one parsed event,
        and no error (a stalled-but-clean turn is still `ok` — the oracle's call)."""
        return self.result.ok and bool(self.events) and not self.errors

    @property
    def _result_event(self) -> dict | None:
        """The final `result` event, if any (carries totals + success flag)."""
        for event in reversed(self.events):
            if event.get("type") == "result":
                return event
        return None

    @property
    def session_id(self) -> str | None:
        """The claude session id (top-level ``session_id``), for `--resume`."""
        for event in self.events:
            sid = event.get("session_id")
            if sid:
                return str(sid)
        return None

    @property
    def model(self) -> str | None:
        """The model claude actually resolved (e.g. "claude-opus-4-8[1m]").

        Read from the ``system`` init event, which reports the resolved model even
        when it was left to the install default — so the baseline always *tracks*
        which model ran, not just which was requested. Falls back to an assistant
        message's ``model`` (skipping the ``<synthetic>`` placeholder emitted on
        some non-model turns)."""
        for event in self.events:
            if event.get("type") == "system" and (m := event.get("model")):
                return str(m)
        for event in self.events:
            if event.get("type") == "assistant":
                m = event.get("message", {}).get("model")
                if m and m != "<synthetic>":
                    return str(m)
        return None

    def _assistant_blocks(self) -> list[dict]:
        """Every content block across all assistant message events, in order."""
        blocks: list[dict] = []
        for event in self.events:
            if event.get("type") != "assistant":
                continue
            content = event.get("message", {}).get("content")
            if isinstance(content, list):
                blocks += [b for b in content if isinstance(b, dict)]
        return blocks

    @property
    def assistant_text(self) -> str:
        """Concatenated text blocks across assistant turns (newline-joined).

        Falls back to the final `result` event's `result` text when no streamed
        text blocks were captured, so the stall/claim oracle always has something
        to read."""
        chunks = [
            text
            for block in self._assistant_blocks()
            if block.get("type") == "text" and (text := str(block.get("text", "")).strip())
        ]
        if chunks:
            return "\n".join(chunks)
        res = self._result_event
        if res and not res.get("is_error"):
            return str(res.get("result", "")).strip()
        return ""

    @property
    def _tool_error_ids(self) -> set[str]:
        """`tool_use_id`s whose `tool_result` reported `is_error` true."""
        bad: set[str] = set()
        for event in self.events:
            if event.get("type") != "user":
                continue
            content = event.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("is_error")
                    and (tid := block.get("tool_use_id"))
                ):
                    bad.add(str(tid))
        return bad

    @property
    def tool_calls(self) -> list[dict]:
        """Each `tool_use` block, re-shaped to the opencode tool-call dict shape.

        Status is `error` when the matching `tool_result` reported `is_error`,
        else `completed` — so the shared oracle's tool-error branch works."""
        bad = self._tool_error_ids
        calls: list[dict] = []
        for block in self._assistant_blocks():
            if block.get("type") != "tool_use":
                continue
            call_id = str(block.get("id", ""))
            status = "error" if call_id in bad else "completed"
            calls.append(
                {"tool": block.get("name"), "callID": call_id, "state": {"status": status}}
            )
        return calls

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def tokens(self) -> int:
        """Total tokens (input + output) from the final `result` event's usage."""
        res = self._result_event
        usage = res.get("usage", {}) if res else {}
        if not isinstance(usage, dict):
            return 0
        return int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)

    @property
    def cost(self) -> float:
        """The turn's `total_cost_usd` from the final `result` event."""
        res = self._result_event
        return float(res.get("total_cost_usd", 0) or 0) if res else 0.0

    @property
    def errors(self) -> list[dict]:
        """Failed-turn signals: a `result` event with `is_error` true (or a
        non-`success` subtype), plus any explicit `error` event."""
        out: list[dict] = []
        for event in self.events:
            t = event.get("type")
            if t == "error":
                out.append(event)
            elif t == "result" and (
                event.get("is_error") or event.get("subtype", "success") != "success"
            ):
                out.append(event)
        return out

    @property
    def error_summary(self) -> str | None:
        """A human-readable summary of the first error, or None."""
        if not self.errors:
            return None
        err = self.errors[0]
        # A failed `result` event carries the detail in `result`/`subtype`; an
        # explicit `error` event carries it under `error`.
        detail = err.get("result") or err.get("error") or err.get("subtype")
        return str(detail) if detail is not None else "error"


def claude_run(
    runner: Runner,
    name: str,
    prompt: str,
    *,
    session: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    skip_permissions: bool = False,
    workspace: str | Path | None = None,
    env_file: str | Path | None = None,
) -> ClaudeTurn:
    """Drive one headless `claude -p --output-format stream-json` turn in `name`.

    The in-sandbox Claude Code counterpart of `opencode_run`, with a `TurnFn`-
    compatible signature so the level runners can drive either agent. `session`
    continues a session (`--resume`), `skip_permissions` adds
    `--dangerously-skip-permissions`, and `workspace` sets the in-VM exec cwd
    (`-w`) so writes land in the mounted workspace the oracle probes.

    `model` pins claude's model via `--model` (an alias like "opus"/"sonnet" or a
    full id like "claude-opus-4-8") — the counterpart of opencode's `-m`, so the
    baseline controls which model runs instead of inheriting the install default
    (which varies in cost/latency). When omitted, claude uses its default and the
    actual resolved model is still tracked via `ClaudeTurn.model`. `agent` is
    accepted for interface parity but ignored (claude has no opencode `--agent`).

    `env_file` is passed to `docker sandbox exec` as `--env-file` — REQUIRED for
    auth: unlike opencode (which reads Ollama from `opencode.jsonc`), claude needs
    `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` in its exec environment, and a
    bare `docker sandbox exec` inherits none. danno injects this only on
    interactive `launch`, so the baseline builds the file itself (see
    `baseline._build_claude_auth_env_file`). The secret stays in the chmod-600
    file, never on the command line.

    Returns the parsed events alongside the raw capture — never raises on a
    non-zero exit, since a stalled/errored turn is the signal the battery
    measures.
    """
    cmd = [*sandbox_cli.base(), "exec"]
    if workspace is not None:
        cmd += ["-w", str(workspace)]
    if env_file is not None:
        cmd += ["--env-file", str(env_file)]
    cmd += [name, "claude", CLAUDE_PRINT_FLAG, CLAUDE_FORMAT_FLAG, CLAUDE_FORMAT_VALUE, "--verbose"]
    if model is not None:
        cmd += [CLAUDE_MODEL_FLAG, model]
    if skip_permissions:
        cmd.append(CLAUDE_SKIP_PERMISSIONS_FLAG)
    if session is not None:
        cmd += [CLAUDE_RESUME_FLAG, session]
    cmd.append(prompt)
    result = runner.capture(cmd)
    return ClaudeTurn(result=result, events=parse_events(result.stdout), raw=result.stdout)


@dataclass
class ClaurstTurn:
    """One captured `claurst -p --output-format stream-json` turn, parsed.

    Claurst's stream-json is JSONL like the others but its OWN schema (M0 spike,
    claurst 0.1.5), so this normalises it onto the shared `Turn` read surface:

    - a **text_delta** event (`type == "text_delta"`) carries a fragment of the
      assistant text at `text` (sub-word deltas — concatenated WITHOUT separators);
    - a **tool_start** event (`type == "tool_start"`) carries `tool` (the tool
      name) — claurst emits no callID and no per-call status in this mode;
    - a final **result** event (`type == "result"`) carries `cost_usd` and `usage`
      (token counts; 0 for Ollama), and on a tool/agent error a `result` string;
    - an **error** event (`type == "error"`) carries the failure string at `error`
      (e.g. `"API error: [ollama] Model not found: unknown"`), with exit code 1.

    Claurst exposes **no session id** in headless Ollama mode (`session_id` is
    always None → each scripted turn is independent), and tokens/cost are 0 for
    local models. Tool calls are re-shaped to ``{"tool": name, "callID": None,
    "state": {"status": "completed"}}`` so the shared oracle/reporter branches work;
    claurst reports no per-call status, so claurst turns are graded by side effect.
    """

    result: CaptureResult
    events: list[dict] = field(default_factory=list)
    raw: str = ""

    @property
    def ok(self) -> bool:
        """True iff the turn ran cleanly: zero exit, ≥1 parsed event, no error
        event (a stalled-but-clean turn is still `ok` — the oracle's call)."""
        return self.result.ok and bool(self.events) and not self.errors

    @property
    def session_id(self) -> str | None:
        """None — claurst exposes no session id in headless Ollama mode (M0)."""
        return None

    @property
    def assistant_text(self) -> str:
        """Concatenated `text_delta` fragments (no separator — they are sub-word
        deltas), stripped. Falls back to the `result` event's `result` text."""
        text = "".join(
            str(event.get("text", "")) for event in self.events if event.get("type") == "text_delta"
        ).strip()
        if text:
            return text
        for event in reversed(self.events):
            if event.get("type") == "result" and not self.errors:
                return str(event.get("result", "")).strip()
        return ""

    @property
    def tool_calls(self) -> list[dict]:
        """Each `tool_start` event, re-shaped to the shared tool-call dict shape.

        Claurst emits no callID/status per call, so each gets a synthetic
        `completed` status; the real signal for claurst is the workspace side
        effect, which the oracle composes with this count."""
        return [
            {"tool": event.get("tool"), "callID": None, "state": {"status": "completed"}}
            for event in self.events
            if event.get("type") == "tool_start"
        ]

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def _result_event(self) -> dict | None:
        for event in reversed(self.events):
            if event.get("type") == "result":
                return event
        return None

    @property
    def tokens(self) -> int:
        """Total tokens (input + output) from the `result` event's usage (0 for
        Ollama — claurst reports no token counts for local models)."""
        res = self._result_event
        usage = res.get("usage", {}) if res else {}
        if not isinstance(usage, dict):
            return 0
        return int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)

    @property
    def cost(self) -> float:
        """The turn's `cost_usd` from the `result` event (0 for local models)."""
        res = self._result_event
        return float(res.get("cost_usd", 0) or 0) if res else 0.0

    @property
    def errors(self) -> list[dict]:
        """Every `error` event (provider/transport failures, exit code 1)."""
        return [event for event in self.events if event.get("type") == "error"]

    @property
    def error_summary(self) -> str | None:
        """A human-readable summary of the first error event, or None."""
        if not self.errors:
            return None
        return str(self.errors[0].get("error", "error event"))


def _claurst_script(
    claurst_cmd: str, *, upstream_port: int = CLAURST_RELAY_DEFAULT_UPSTREAM_PORT
) -> str:
    """Wrap a claurst invocation with the in-VM Ollama relay (see relay constants).

    Returns a `bash -lc` script that writes the relay to a temp file, starts it on
    127.0.0.1:11434, waits for readiness, runs `claurst_cmd` (with `OLLAMA_HOST`
    pointed at the relay), and kills the relay on exit. The relay is co-located in
    this one exec because execs reap their children — it cannot outlive the turn.
    Only claurst's stdout reaches the capture (relay log + readiness probe are
    redirected away), so the JSONL parser sees a clean stream.

    `upstream_port` is the host port the relay re-issues to (via the egress proxy):
    the real Ollama port by default, or a `--capture` recording proxy's port — the
    relay reads it from `DANNO_RELAY_UPSTREAM_PORT`. The relay's own LISTEN port stays
    11434 (claurst's `OLLAMA_HOST`); only the upstream changes.
    """
    heredoc = f"cat > \"$RELAY_PY\" <<'DANNO_RELAY_EOF'\n{_OLLAMA_RELAY_SOURCE}\nDANNO_RELAY_EOF"
    # The upstream port is computed (capture proxy vs real Ollama) so it is set inline. The
    # relay-read timeout defaults to CLAURST_RELAY_DEFAULT_TIMEOUT but honors an inherited
    # value first (`${VAR:-default}`), so a [env]/host `DANNO_RELAY_TIMEOUT` can override it.
    _t = CLAURST_RELAY_TIMEOUT_ENV
    timeout_env = f'{_t}="${{{_t}:-{CLAURST_RELAY_DEFAULT_TIMEOUT}}}"'
    return (
        "RELAY_PY=$(mktemp /tmp/danno-relay-XXXXXX.py)\n"
        f"{heredoc}\n"
        f"{CLAURST_RELAY_UPSTREAM_ENV}={upstream_port} "
        f"{timeout_env} "
        'python3 "$RELAY_PY" 11434 >/tmp/danno-ollama-relay.log 2>&1 &\n'
        "DANNO_RELAY_PID=$!\n"
        "trap 'kill $DANNO_RELAY_PID 2>/dev/null' EXIT\n"
        "for _ in $(seq 1 40); do "
        "curl -fsS --noproxy 127.0.0.1 http://127.0.0.1:11434/api/tags >/dev/null 2>&1 "
        "&& break; sleep 0.25; done\n"
        f"{claurst_cmd}\n"
    )


def claurst_run(
    runner: Runner,
    name: str,
    prompt: str,
    *,
    session: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    skip_permissions: bool = False,
    workspace: str | Path | None = None,
    env_file: str | Path | None = None,
    capture_port: int | None = None,
) -> ClaurstTurn:
    """Drive one headless `claurst -p --output-format stream-json` turn in `name`.

    The in-sandbox Claurst counterpart of `opencode_run`, with a `TurnFn`-compatible
    signature so the level runners can drive it unchanged. `model` pins the model
    (`-m ollama/<tag>`, the same ref the sweep passes opencode); `skip_permissions`
    adds `--dangerously-skip-permissions`; `workspace` becomes claurst's `--cwd` so
    writes land in the mounted workspace the oracle probes; `session` maps to
    `--resume` (a no-op in practice — claurst exposes no session id, M0). `agent` is
    accepted for interface parity but ignored (claurst has no opencode `--agent`).

    For a LOCAL Ollama model the turn runs RELAY-FREE (plan W3 + W6): claurst dials host
    Ollama at `host.docker.internal:11434` directly through the sandbox egress proxy,
    which the fork build's client honors (verified S1, sbx + legacy). Under `--capture`
    it dials the host-side recording proxy the same way
    (`host.docker.internal:<capture_port>`, opened in egress by `capture_allow_hosts`) —
    still no in-VM relay. A CLOUD model (`nvidia/…`) likewise dials the provider directly
    through `HTTPS_PROXY`, with the provider key supplied via `env_file`. `env_file` is
    forwarded to the sandbox exec either way (local Ollama needs none).

    Returns the parsed events alongside the raw capture — never raises on a non-zero
    exit, since a stalled/errored turn is the signal the battery measures.
    """
    argv = ["claurst", CLAURST_PRINT_FLAG, CLAURST_FORMAT_FLAG, CLAURST_FORMAT_VALUE]
    if model is not None:
        argv += [CLAURST_MODEL_FLAG, model]
    if skip_permissions:
        argv.append(CLAURST_SKIP_PERMISSIONS_FLAG)
    if workspace is not None:
        argv += [CLAURST_CWD_FLAG, str(workspace)]
    if session is not None:
        argv += [CLAURST_RESUME_FLAG, session]
    argv.append(prompt)
    cmd = [*sandbox_cli.base(), "exec"]
    if env_file is not None:
        cmd += ["--env-file", str(env_file)]
    # A LOCAL Ollama model (`ollama/…`, or claurst's default when `model` is None) is now
    # fully RELAY-FREE (plan W3 + W6): claurst honors the egress proxy, so it dials host
    # Ollama at host.docker.internal directly — and under `--capture` it dials the host-side
    # recording proxy the same way (`host.docker.internal:<capture_port>`, allowed in egress
    # by `capture_allow_hosts`), no in-VM relay. A CLOUD model (`nvidia/…`) dials the
    # provider directly through `HTTPS_PROXY` (honored by the fork build), with the key
    # supplied via `env_file` — a plain argv, no `bash -lc`, no `OLLAMA_HOST`.
    is_local = model is None or model.startswith("ollama/")
    if is_local:
        ollama_host = _claurst_ollama_host(capture_port)
        cmd += [name, "bash", "-lc", f"OLLAMA_HOST={ollama_host} {shlex.join(argv)}"]
    else:
        cmd += [name, *argv]
    result = runner.capture(cmd)
    return ClaurstTurn(result=result, events=parse_events(result.stdout), raw=result.stdout)


@dataclass
class OccTurn:
    """One captured `node <occ>/v2/src/index.mjs -p --output-format stream-json` turn.

    occ's stream-json is JSONL (one `console.log(JSON.stringify(event))` per event) with
    its OWN schema (pinned against the ruvnet/open-claude-code v2 snapshot, 2026-07-02),
    normalised here onto the shared `Turn` read surface:

    - an **assistant** event (`type == "assistant"`) carries a chunk of assistant text
      at `content` (concatenated across events — NOT sub-word deltas like claurst);
    - a **tool_progress** event (`type == "tool_progress"`) carries `tool` (name) and
      `status` (`"running"` when the tool starts) — occ emits one per tool invocation;
    - a **result** event (`type == "result"`) carries `tool` + `result` (the tool's
      output) — a per-tool terminal, NOT a turn-level usage block (stream-json emits no
      usage summary; only occ's `json` mode does, which danno does not use);
    - a **stop** event (`type == "stop"`) carries `reason` (`"end_turn"`, `"max_turns"`,
      `"max_recursion"`); an error-ish reason marks a degraded turn;
    - an **error** event (`type == "error"`) carries the failure string at `message`
      (NB: `message`, not claurst's `error`).

    occ exposes no session id in this mode (`session_id` is always None → each scripted
    turn is independent) and stream-json carries no token/cost totals, so `tokens`/`cost`
    are 0. Tool calls are re-shaped to ``{"tool": name, "callID": None, "state":
    {"status": …}}`` so the shared oracle/reporter branches work; the status is
    `"completed"` unless the tool's terminal signalled an error.
    """

    result: CaptureResult
    events: list[dict] = field(default_factory=list)
    raw: str = ""

    @property
    def ok(self) -> bool:
        """True iff the turn ran cleanly: zero exit, ≥1 parsed event, no error event."""
        return self.result.ok and bool(self.events) and not self.errors

    @property
    def session_id(self) -> str | None:
        """None — occ exposes no session id in headless stream-json mode."""
        return None

    @property
    def assistant_text(self) -> str:
        """Concatenated `assistant` event `content` (joined by newlines), stripped."""
        return "\n".join(
            str(event.get("content", ""))
            for event in self.events
            if event.get("type") == "assistant" and event.get("content")
        ).strip()

    @property
    def tool_calls(self) -> list[dict]:
        """Each `tool_progress` event, re-shaped to the shared tool-call dict shape.

        occ emits no callID; a tool is marked `error` only if a later `result`/`error`
        event names it as failed, else `completed`. The real signal for occ (like
        claurst) is the workspace side effect, which the oracle composes with this count.
        """
        return [
            {"tool": event.get("tool"), "callID": None, "state": {"status": "completed"}}
            for event in self.events
            if event.get("type") == "tool_progress"
        ]

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def tokens(self) -> int:
        """0 — occ's stream-json emits no token totals (only its `json` mode does)."""
        return 0

    @property
    def cost(self) -> float:
        """0.0 — occ's stream-json emits no cost totals."""
        return 0.0

    @property
    def errors(self) -> list[dict]:
        """Every `error` event, plus a `stop` whose `reason` is an error condition."""
        errs = [event for event in self.events if event.get("type") == "error"]
        errs += [
            event
            for event in self.events
            if event.get("type") == "stop"
            and str(event.get("reason", "")) in ("max_turns", "max_recursion")
        ]
        return errs

    @property
    def error_summary(self) -> str | None:
        """A human-readable summary of the first error, or None."""
        for event in self.errors:
            if event.get("type") == "error":
                return str(event.get("message", "error event"))
            return f"stopped: {event.get('reason')}"
        return None


def occ_model_target(model_ref: str | None) -> tuple[str | None, bool]:
    """Translate a sweep/interactive model ref into occ's `-m` value + locality.

    The sweep passes opencode-format `<backend>/<tag>` refs for EVERY agent (e.g.
    `ollama/gemma3:27b`), but occ's `-m` wants the provider's bare model id and routes on
    OPENAI_BASE_URL (the fork's native detectProvider). This is the ONE place that knows
    the `<backend>/<tag>` ↔ occ mapping, so drift is contained. Returns `(m_value, is_local)`:

    - `None` → `(None, True)`: interactive with no `-m`; occ falls back to its own default
      (treated as local — the relay path).
    - `ollama/<tag>` → `(<tag>, True)`: strip the `ollama/` prefix; the relay supplies
      OPENAI_BASE_URL so the bare Ollama tag routes correctly (no `gpt-` alias needed).
    - any other `<backend>/<tag>` (or a bare ref) → `(part after the first '/', or the ref
      itself, False)`: cloud; the config-aware caller supplies the provider's
      OPENAI_BASE_URL + OPENAI_API_KEY via the env-file (see `sandbox.occ_cloud_env_lines`).
    """
    if model_ref is None:
        return None, True
    is_local = model_ref.startswith("ollama/")
    m_value = model_ref.split("/", 1)[1] if "/" in model_ref else model_ref
    return m_value, is_local


def occ_run(
    runner: Runner,
    name: str,
    prompt: str,
    *,
    session: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    skip_permissions: bool = False,
    workspace: str | Path | None = None,
    env_file: str | Path | None = None,
    capture_port: int | None = None,
    max_turns: int = OCC_DEFAULT_MAX_TURNS,
) -> OccTurn:
    """Drive one headless occ turn in sandbox `name` (the occ counterpart of `claurst_run`).

    `TurnFn`-compatible so the level runners drive it unchanged. `model` is a
    `<backend>/<tag>` ref (as the sweep passes every agent); `occ_model_target` translates
    it to occ's `-m` value + locality. `skip_permissions` is a no-op for interface parity
    (occ always runs `--permission-mode bypassPermissions` headless — the sandbox is the
    isolation boundary); `session` is accepted but unused (occ exposes no session id);
    `workspace` becomes the exec cwd (`-w`) so writes land where the oracle probes.

    LOCAL Ollama: wrapped in the shared `_claurst_script` relay bracket (reused verbatim),
    with `OPENAI_BASE_URL`→the relay + a dummy `OPENAI_API_KEY` + `CLAUDE_CODE_STREAMING=0`
    set inline. `capture_port` redirects the relay's upstream at a `--capture` proxy.
    CLOUD: no relay; only `CLAUDE_CODE_STREAMING=0` is set inline. The fork's global undici
    dispatcher honors HTTPS_PROXY (read from `env_file`), so no shim is needed; the
    provider's `OPENAI_BASE_URL` + `OPENAI_API_KEY` also come from `env_file` (built
    config-side, see `occ_cloud_env_lines`).

    Returns the parsed events alongside the raw capture — never raises on a non-zero exit.
    """
    m_value, is_local = occ_model_target(model)
    node_argv = [
        "node",
        OCC_ENTRY,
        OCC_FORMAT_FLAG,
        OCC_FORMAT_VALUE,
        OCC_PERMISSION_FLAG,
        OCC_PERMISSION_VALUE,
    ]
    if m_value is not None:
        node_argv += [OCC_MODEL_FLAG, m_value]
    node_argv += [OCC_MAX_TURNS_FLAG, str(max_turns), OCC_PRINT_FLAG, prompt]

    cmd = [*sandbox_cli.base(), "exec"]
    if workspace is not None:
        cmd += ["-w", str(workspace)]
    if env_file is not None:
        cmd += ["--env-file", str(env_file)]
    if is_local:
        # Local: the relay bracket (reused verbatim from claurst) + occ's OpenAI env set
        # inline; occ reaches the relay at 127.0.0.1:11434 as an OpenAI-compatible endpoint.
        occ_cmd = f"{OCC_LOCAL_OPENAI_ENV} {OCC_STREAMING_ENV} {shlex.join(node_argv)}"
        upstream_port = (
            CLAURST_RELAY_DEFAULT_UPSTREAM_PORT if capture_port is None else capture_port
        )
        cmd += [name, "bash", "-lc", _claurst_script(occ_cmd, upstream_port=upstream_port)]
    else:
        # Cloud: no relay. The fork's global undici dispatcher reads HTTPS_PROXY from the
        # env-file (no NODE_OPTIONS shim). The provider base URL + key also ride the
        # env-file (OPENAI_BASE_URL / OPENAI_API_KEY, see `occ_cloud_env_lines`).
        occ_cmd = f"{OCC_STREAMING_ENV} {shlex.join(node_argv)}"
        cmd += [name, "bash", "-lc", occ_cmd]
    result = runner.capture(cmd)
    return OccTurn(result=result, events=parse_events(result.stdout), raw=result.stdout)


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
