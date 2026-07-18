"""Tier B fixtures for the runaway-gate validation suites (GV2/GV3).

Wires the deterministic stub AI (`book_em_danno.stubai`) to a REAL Docker sandbox exactly
where host Ollama sits today: the sandboxed harness dials the always-on capture proxy
(`host.docker.internal:<PROXY_PORT>`), which feeds a live `GateTally` and forwards to the
stub on the host (`127.0.0.1:<STUB_PORT>`). No new plumbing — the same `--allow-host` +
`host.docker.internal` rewrite the `--capture` path already uses. The harness executes
real tool calls inside the sandbox (security invariant: the HUT never runs on the host);
only the "model" is stubbed.

    host                                  │ Docker sandbox VM
    stub AI (STUB_PORT) ◄─ capture proxy ◄┼─ harness (opencode/claurst)
       │  (script → wire dialect)  (PROXY  │    OLLAMA_BASE_URL / OPENAI_BASE_URL
       └─ transcript.jsonl          PORT,  │    → host.docker.internal:PROXY_PORT
                                    tally) │

LIVE-VERIFIED 2026-07-16 (full `pytest -m slow` green across opencode/claurst). The
first live run confirmed the three things the wiring depended on:
  - the stub's loop tool matches a tool each harness actually advertises (no unknown/
    hallucinated-tool substitution) — see `LOOP_TOOL`;
  - claurst local routing dials the proxy (backend named `ollama` + the in-VM relay —
    memory `sbx-migration-w1-w2-done-pr76-ready`);
  - opencode honors `agent.steps` at the template's version (V1 runner) — the V5 canary.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from sandbox_runtime import sandbox_runtime_down

from book_em_danno.capture.gate import GateTally
from book_em_danno.capture.proxy import CaptureProxyConfig, capture_proxy
from book_em_danno.commands import ollama, sandbox
from book_em_danno.config.generate import generate
from book_em_danno.config.schema import DannoConfig, Defaults, Model, OllamaBackend
from book_em_danno.core.exec import GateWatch, Runner
from book_em_danno.stubai import Stub, StubConfig, stub_ai
from danno_validator.driver import Turn
from danno_validator.harnesses import WireProtocol, get
from danno_validator.suites.aut import run_turn_for
from danno_validator.suites.base import _reap_harness
from danno_validator.suites.config import ResolvedGates, watchdog_max_turns

# Fixed host ports: the proxy port is baked into provisioning (allow-list + config
# base_url), so it must be stable across the per-test stub/proxy restarts; the stub port is
# the proxy's upstream. Chosen off the common Ollama/opencode ranges.
PROXY_PORT = 11455
STUB_PORT = 11456
# The model tag the harness dials (`-m ollama/<tag>`). The stub answers regardless of tag;
# it just has to be a syntactically valid ref that matches the stub's /api/tags entry.
MODEL_TAG = "stub"
# The tool the `tool_loop`/`tool_call` stub emits each round, keyed by the harness's wire
# protocol. It MUST be a tool the harness actually advertises so the harness executes it and
# loops (not an "unknown tool" substitution). CHAT harnesses (opencode/claurst) advertise a
# `bash` tool with a `command` string; codex (RESPONSES) advertises `exec_command` with a
# `cmd` string (Phase-0 spike — `.docs/codex-integration.md` Q3). CONFIRM on first live run
# per new harness (see module docstring).
_LOOP_TOOLS: dict[WireProtocol, tuple[str, dict[str, object]]] = {
    WireProtocol.CHAT: ("bash", {"command": "true"}),
    WireProtocol.RESPONSES: ("exec_command", {"cmd": "true"}),
}
# CHAT default, kept as module constants for the opencode-only drift canary (V5). Per-harness
# callers use `loop_tool(harness)` instead.
LOOP_TOOL, LOOP_TOOL_ARGS = _LOOP_TOOLS[WireProtocol.CHAT]


def loop_tool(harness: str) -> tuple[str, dict[str, object]]:
    """The `(tool_name, args)` the stub must emit to make `harness` execute a tool and loop.

    Keyed on the registered harness's `wire_protocol`, so adding a harness needs only a new
    `_LOOP_TOOLS` row (not a name branch). Fails loud for a harness with no loop tool wired."""
    proto = get(harness).wire_protocol
    try:
        return _LOOP_TOOLS[proto]
    except KeyError:
        raise ValueError(f"no loop tool wired for harness '{harness}' ({proto})") from None


# Probe the runtime danno would actually use (`sbx ls` for sbx, else `docker info`),
# NOT the standalone `docker` daemon — which can be down on an sbx host while sbx is up.
DOCKER_DOWN = sandbox_runtime_down()
OLLAMA_DOWN = not ollama.reachable()  # only V5's live-diff row needs this; V3/V4 do not


def gen_config(harness: str) -> DannoConfig:
    """A minimal danno config whose single Ollama model dials the capture proxy instead of
    real Ollama. `default_agent`/`build` must name a defined agent or opencode rejects the
    session. claurst requires the backend to be named literally `ollama` to route local."""
    return DannoConfig(
        defaults=Defaults(default_agent="build"),
        backends={
            "ollama": OllamaBackend(
                kind="ollama",
                base_url=f"http://host.docker.internal:{PROXY_PORT}/v1",
            )
        },
        models={
            "stub": Model(
                backend="ollama",
                tag=MODEL_TAG,
                reasoning_effort="none",  # avoid the #21903 reasoning-field hang path
                context_budget=32000,
                output_limit=8192,
            )
        },
        agents={"build": "stub"},
    )


@dataclass
class ScriptedBackend:
    """One host-side stub+proxy pairing for a cell: the stub (script + transcript) and the
    live tally the watchdog polls."""

    stub: Stub
    tally: GateTally
    capture_file: Path


@contextmanager
def scripted_backend(script: list, tmp_path: Path) -> Iterator[ScriptedBackend]:
    """Start the stub (STUB_PORT) + capture proxy (PROXY_PORT, fresh tally) for one cell.
    Both are host-side and cheap, so they restart per test while the sandbox is provisioned
    once. The proxy's upstream is the stub; its tally is what Gate 1/2 poll."""
    stub_cfg = StubConfig(script=script, transcript_file=tmp_path / "stub.jsonl", port=STUB_PORT)
    with stub_ai(stub_cfg) as stub:
        tally = GateTally()
        capture_file = tmp_path / "capture.jsonl"
        proxy_cfg = CaptureProxyConfig(
            upstream=f"http://127.0.0.1:{stub.port}",
            capture_file=capture_file,
            port=PROXY_PORT,
            tally=tally,
        )
        with capture_proxy(proxy_cfg):
            yield ScriptedBackend(stub=stub, tally=tally, capture_file=capture_file)


@contextmanager
def provisioned_sandbox(name: str, harness: str, tmp_path: Path) -> Iterator[Path]:
    """Generate the proxy-dialing config and provision a real sandbox once, wired to allow
    the proxy port. Yields the workspace/target dir; tears the sandbox down on exit."""
    from danno_validator.suites.bench import _seed_opencode_config

    generate(gen_config(harness), tmp_path, apply=True)
    _seed_workspace(tmp_path)
    teardown_sandbox(name)
    try:
        sandbox.provision(
            Runner(apply=True),
            name,
            tmp_path,
            harness=harness,
            # The proxy rewrites host.docker.internal -> localhost before the allow-list
            # match, so the rule names localhost:<proxy port>.
            allow_hosts=(f"localhost:{PROXY_PORT}",),
        )
        # Mirror the real bench flow (bench.py:695): opencode reads its model registry from
        # `.opencode/opencode.jsonc`, which bench (re)seeds POST-provision with title-gen
        # disabled. A bare pre-provision generate leaves title-gen enabled, and opencode then
        # makes 0 model calls (the "Model not found: ollama/<tag>" path _seed_opencode_config
        # was written to fix). No-op for claurst (it routes via the in-VM relay).
        _seed_opencode_config(gen_config(harness), harness, tmp_path)
        yield tmp_path
    finally:
        teardown_sandbox(name)


def run_scripted_turn(
    runner: Runner,
    name: str,
    backend: ScriptedBackend,
    prompt: str,
    *,
    harness: str,
    gates: ResolvedGates,
    workspace: Path,
) -> tuple[Turn, GateWatch]:
    """Drive one harness turn under the runaway-gate watchdog — the exact seam `suites.base.
    run_cell` uses: `runner.watching(probe=tally, ...)` wraps the harness turn fn, so a breach
    kills the cell and lands on `watch.breach`. Option B: the harness's NATIVE cap is set to
    the raw `gates.max_turns` and the external watchdog sits a grace margin above it
    (`watchdog_max_turns`), so a cap-honoring harness (claurst) stops itself first.

    Built via the canonical `run_turn_for` (not the raw driver fns) so claurst gets its
    two local-routing knobs bench binds too: `capture_port=PROXY_PORT` points its in-VM
    Ollama relay at the stub proxy (else it dials the default 11434 → 0 stub calls), and
    `max_turns` is its `--max-turns` polite-stop. opencode ignores both — it dials the proxy
    via its seeded backend `base_url` and relies on the external Gate 1."""
    turn_fn = run_turn_for(harness, None, capture_port=PROXY_PORT, max_turns=gates.max_turns)
    with runner.watching(
        probe=backend.tally,
        max_turns=watchdog_max_turns(gates.max_turns),
        max_tokens=gates.max_tokens,
        timeout_s=gates.timeout_s,
        on_kill=lambda: _reap_harness(runner, name),
    ) as watch:
        turn = turn_fn(
            runner,
            name,
            prompt,
            agent="build",
            model=f"ollama/{MODEL_TAG}",
            skip_permissions=True,
            workspace=workspace,
        )
    return turn, watch


def surviving_harness_pids(name: str) -> list[str]:
    """PIDs of any harness process still alive in the sandbox after a kill+reap — the
    post-kill invariant is that this is empty (`ps` filtered by the reap pattern).

    The alternatives are bracketed (`[o]pencode`, not `opencode`) so the pattern string does
    NOT contain the literal names: otherwise `pgrep -f` matches its OWN `bash -lc "pgrep -f
    'opencode|...'"` wrapper (whose cmdline carries the pattern), returning a fresh self-match
    PID every call — a false 'surviving harness' on a sandbox with nothing running. Verified
    2026-07-15: broad pattern → a pid on an idle sandbox; bracketed → none. Same self-match
    class as the deferred reaper F7."""
    result = subprocess.run(
        [
            *_sbx_base(),
            "exec",
            name,
            "bash",
            "-lc",
            r"pgrep -f '[o]pencode|[c]laurst|[i]ndex\.mjs' || true",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return [line for line in result.stdout.split() if line.strip()]


def teardown_sandbox(name: str) -> None:
    from book_em_danno.commands import sandbox_cli

    subprocess.run([*_sbx_base(), "stop", name], capture_output=True, check=False)
    # `sbx rm` prompts for confirmation and aborts on a non-tty (this exec path is headless),
    # so it needs `--force` — use the canonical argv. A bare `sbx rm` silently no-ops, leaving
    # an orphaned sandbox that provision then "already exists — skipping create"s, reusing a
    # dead VM so every inference call vanishes (0-round false failures across the matrix).
    subprocess.run(sandbox_cli.rm_argv(name), capture_output=True, check=False)


def _sbx_base() -> list[str]:
    from book_em_danno.commands import sandbox_cli

    return list(sandbox_cli.base())


def _seed_workspace(target: Path) -> None:
    """A tiny file so read/list tool calls have something real to act on."""
    (target / "notes.txt").write_text(
        "hello from the gate-validation workspace\n", encoding="utf-8"
    )


def model_present(tag: str) -> bool:
    """Whether a real Ollama model is pulled (for V5's opt-in stub-vs-live framing diff)."""
    try:
        with urllib.request.urlopen(f"{ollama.DEFAULT_HOST_URL}/api/tags", timeout=2.0) as resp:
            import json

            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return any(m.get("name") == tag for m in body.get("models", []))


# Shared skip decorator: Tier B needs a working Docker sandbox (host Ollama is replaced by
# the stub, so it is NOT required except for V5's live-diff row).
requires_docker = pytest.mark.skipif(DOCKER_DOWN, reason="Docker sandbox unavailable")


# ---------------------------------------------------------------------------
# #97 — recorded-history well-formedness (wire-shape-aware).
# ---------------------------------------------------------------------------


def _inference_request_bodies(capture_file: Path) -> list[dict]:
    """The decoded JSON request bodies for every inference call the proxy recorded, in order
    (`/chat/completions` for CHAT, `/responses` for RESPONSES)."""
    import json

    bodies: list[dict] = []
    if not capture_file.is_file():
        return bodies
    for line in capture_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec.get("direction") != "request":
            continue
        path = str(rec.get("path", ""))
        if not (path.endswith("/chat/completions") or path.endswith("/responses")):
            continue
        body = rec.get("body")
        if isinstance(body, dict):
            bodies.append(body)
    return bodies


def _chat_messages(body: dict) -> list[dict]:
    msgs = body.get("messages")
    return [m for m in msgs if isinstance(m, dict)] if isinstance(msgs, list) else []


def _responses_input(body: dict) -> list[dict]:
    inp = body.get("input")
    return [i for i in inp if isinstance(i, dict)] if isinstance(inp, list) else []


def _assert_chat_well_formed(bodies: list[dict]) -> None:
    """CHAT (#97): in every follow-up request carrying a tool result, each tool-role message's
    `tool_call_id` resolves to a PRECEDING assistant `tool_calls[].id`, and ≥1 assistant
    message is present — the tool_calls[].id ↔ tool_call_id pairing a malformed replay drops."""
    followups = [b for b in bodies if any(m.get("role") == "tool" for m in _chat_messages(b))]
    assert followups, "no follow-up CHAT request carried a tool result — nothing to check (#97)"
    for body in followups:
        seen_ids: set[str] = set()
        assistant_seen = False
        for msg in _chat_messages(body):
            role = msg.get("role")
            if role == "assistant":
                assistant_seen = True
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict) and tc.get("id"):
                        seen_ids.add(str(tc["id"]))
            elif role == "tool":
                cid = str(msg.get("tool_call_id"))
                assert cid in seen_ids, (
                    f"tool message tool_call_id={cid!r} has no preceding assistant "
                    f"tool_calls[].id (seen: {sorted(seen_ids)}) (#97)"
                )
        assert assistant_seen, "no assistant message in the recorded CHAT history (#97)"


def _assert_responses_well_formed(bodies: list[dict]) -> None:
    """RESPONSES (#97): in every follow-up request carrying a `function_call_output`, each
    output's `call_id` resolves to a PRECEDING `function_call.call_id`, and ≥1 assistant
    turn (a `reasoning`/assistant `message`/`function_call` item) is present — the Responses
    analog of the CHAT pairing (`.docs/codex-integration.md` §history-shape)."""
    followups = [
        b
        for b in bodies
        if any(i.get("type") == "function_call_output" for i in _responses_input(b))
    ]
    assert followups, "no follow-up RESPONSES request carried a function_call_output (#97)"
    for body in followups:
        seen_ids: set[str] = set()
        assistant_seen = False
        for item in _responses_input(body):
            kind = item.get("type")
            if kind == "function_call":
                assistant_seen = True  # a function_call IS an assistant action
                if item.get("call_id"):
                    seen_ids.add(str(item["call_id"]))
            elif kind == "reasoning" or (kind == "message" and item.get("role") == "assistant"):
                assistant_seen = True
            elif kind == "function_call_output":
                cid = str(item.get("call_id"))
                assert cid in seen_ids, (
                    f"function_call_output call_id={cid!r} has no preceding function_call "
                    f"(seen: {sorted(seen_ids)}) (#97)"
                )
        assert assistant_seen, (
            "no assistant/reasoning/function_call item in the RESPONSES history (#97)"
        )


def assert_history_well_formed(harness: str, capture_file: Path) -> None:
    """Assert the recorded request history is well-formed for `harness`'s wire protocol (#97).

    Wire-shape-aware, keyed on `get(harness).wire_protocol`, so a new harness routes to the
    right check by its registry entry rather than a name branch. Called from the clean-finish
    termination cell, whose 3-tool-calls-then-finish script produces a follow-up request that
    replays the tool call(s) + result(s) — exactly the history a malformed replay corrupts."""
    proto = get(harness).wire_protocol
    bodies = _inference_request_bodies(capture_file)
    assert bodies, f"no inference request bodies captured for '{harness}' ({proto}) (#97)"
    if proto is WireProtocol.CHAT:
        _assert_chat_well_formed(bodies)
    elif proto is WireProtocol.RESPONSES:
        _assert_responses_well_formed(bodies)
    else:  # ANTHROPIC (claude) never routes through the capture proxy
        raise ValueError(f"no #97 history assertion for '{harness}' ({proto})")
