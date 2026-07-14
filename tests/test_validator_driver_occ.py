"""Unit tests for the Phase-2 occ driver — `occ_run` command construction (relay-wrapped
`bash -lc` for local Ollama, undici-shim `bash -lc` for cloud), the `occ_model_target`
`<backend>/<tag>` → `-m` translation, and `OccTurn` parsing of occ's `--output-format
stream-json` JSONL onto the shared `Turn` read surface. All subprocess calls are stubbed,
so these run without a Docker daemon (occ itself only runs in the VM). The event shapes
here mirror the ruvnet/open-claude-code v2 stream-json snapshot pinned by the integration
spike (2026-07-02)."""

from __future__ import annotations

import subprocess

import pytest

from book_em_danno.core import exec as exec_mod
from book_em_danno.core.exec import Runner
from danno_validator import driver
from danno_validator.driver import Turn


def _patch_capture(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", returncode: int = 0
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(exec_mod.subprocess, "run", fake_run)
    return calls


# Faithful occ stream-json events: a file-write task emits a request-start marker,
# assistant text chunks, a tool_progress (start), a per-tool result, and a terminal stop.
_START = '{"type":"stream_request_start","turn":1}'
_ASSIST_A = '{"type":"assistant","content":"Writing"}'
_ASSIST_B = '{"type":"assistant","content":"the file."}'
_TOOL = '{"type":"tool_progress","tool":"Write","status":"running"}'
_RESULT = '{"type":"result","tool":"Write","result":"ok"}'
_STOP = '{"type":"stop","reason":"end_turn"}'
_ERROR = '{"type":"error","message":"provider unreachable: connection refused"}'
_STOP_MAXTURNS = '{"type":"stop","reason":"max_turns"}'

_FULL_TURN = "\n".join([_START, _ASSIST_A, _ASSIST_B, _TOOL, _RESULT, _STOP]) + "\n"


def _script(calls: list[list[str]]) -> str:
    """The `bash -lc` script argument of the (single) captured exec."""
    argv = calls[0]
    assert argv[-3:-1] == ["bash", "-lc"]
    return argv[-1]


# ── command construction: local Ollama path ────────────────────────────────────────


def test_occ_run_local_wraps_with_relay_and_openai_env(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "do the thing", model="ollama/gemma3:27b")
    script = _script(calls)
    # occ's mandatory OpenAI env is set inline: the relay base URL, a dummy key, no streaming.
    assert "OPENAI_BASE_URL=http://127.0.0.1:11434/v1" in script
    assert "OPENAI_API_KEY=dummy" in script
    assert "CLAUDE_CODE_STREAMING=0" in script
    # headless flags
    assert "--output-format stream-json" in script
    assert "--permission-mode bypassPermissions" in script
    # the BARE ollama tag reaches -m (no ollama/ prefix leaks onto the command line)
    assert "-m gemma3:27b" in script
    assert "ollama/gemma3:27b" not in script
    assert script.rstrip().endswith("'do the thing'")  # prompt shell-quoted, last arg


def test_occ_run_local_sets_up_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "go", model="ollama/gemma3:27b")
    script = _script(calls)
    assert "mktemp /tmp/danno-relay-" in script  # the shared claurst relay bracket, reused
    assert "ThreadingHTTPServer" in script
    assert "http://127.0.0.1:11434/api/tags" in script  # readiness probe
    assert "trap 'kill $DANNO_RELAY_PID" in script


def test_occ_run_default_relay_upstream_is_real_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "go", model="ollama/x")
    assert "DANNO_RELAY_UPSTREAM_PORT=11434 " in _script(calls)


def test_occ_run_capture_port_redirects_relay_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "go", model="ollama/x", capture_port=54321)
    script = _script(calls)
    assert "DANNO_RELAY_UPSTREAM_PORT=54321 " in script
    assert 'python3 "$RELAY_PY" 11434' in script  # relay still LISTENS on 11434


def test_occ_run_relay_timeout_defaults_and_is_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    # The relay-read timeout is set on the launch line with a generous default (60 min for
    # slow local prefills) but honors an inherited DANNO_RELAY_TIMEOUT so [env]/host can raise
    # it. The relay source itself reads the var with the same default. See Phase 3 / ADR-004.
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "go", model="ollama/x")
    script = _script(calls)
    assert (
        f'DANNO_RELAY_TIMEOUT="${{DANNO_RELAY_TIMEOUT:-{driver.CLAURST_RELAY_DEFAULT_TIMEOUT}}}"'
        in script
    )
    assert 'os.environ.get("DANNO_RELAY_TIMEOUT", "3600")' in script  # relay reads it
    assert driver.CLAURST_RELAY_DEFAULT_TIMEOUT == 3600


def test_occ_run_max_turns_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "go", model="ollama/x")
    assert f"--max-turns {driver.OCC_DEFAULT_MAX_TURNS}" in _script(calls)
    calls2 = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "go", model="ollama/x", max_turns=3)
    assert "--max-turns 3" in _script(calls2)


def test_occ_run_workspace_becomes_exec_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "go", model="ollama/x", workspace="/repo")
    argv = calls[0]
    assert argv[:5] == ["docker", "sandbox", "exec", "-w", "/repo"]


# ── command construction: cloud path ────────────────────────────────────────────────


def test_occ_run_cloud_no_shim_no_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(
        Runner(), "box", "go", model="nvidia/qwen/qwen3.5-397b", env_file="/tmp/danno-env"
    )
    argv = calls[0]
    # provider base URL + key ride the env-file (built config-side), forwarded on the exec.
    assert argv[:5] == ["docker", "sandbox", "exec", "--env-file", "/tmp/danno-env"]
    script = _script(calls)
    # The fork's global dispatcher reads HTTPS_PROXY from the env-file — no NODE_OPTIONS shim.
    assert "NODE_OPTIONS" not in script
    assert "CLAUDE_CODE_STREAMING=0" in script
    # no relay on the cloud path, and no local base-URL override
    assert "ThreadingHTTPServer" not in script
    assert "OPENAI_BASE_URL=http://127.0.0.1:11434" not in script
    # the bare cloud model id reaches -m (backend prefix stripped)
    assert "-m qwen/qwen3.5-397b" in script


# ── occ_model_target translation ────────────────────────────────────────────────────


def test_occ_model_target_local_strips_prefix() -> None:
    assert driver.occ_model_target("ollama/gemma3:27b") == ("gemma3:27b", True)


def test_occ_model_target_cloud_strips_backend() -> None:
    assert driver.occ_model_target("nvidia/qwen/qwen3.5-397b") == ("qwen/qwen3.5-397b", False)


def test_occ_model_target_none_is_local_no_flag() -> None:
    assert driver.occ_model_target(None) == (None, True)


def test_occ_run_none_model_omits_m_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "go", model=None)
    assert " -m " not in _script(calls)


def test_occ_run_ignores_agent_and_skip_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    # agent/skip_permissions exist only for TurnFn signature parity.
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "go", model="ollama/x", agent="build", skip_permissions=True)
    assert "--agent" not in _script(calls)


# ── OccTurn parsing ─────────────────────────────────────────────────────────────────


def test_occ_turn_parses_full_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout=_FULL_TURN)
    turn = driver.occ_run(Runner(), "box", "make a file", model="ollama/x")
    assert isinstance(turn, Turn)  # satisfies the shared read surface
    assert turn.ok is True
    assert turn.session_id is None  # occ exposes no session id
    assert turn.assistant_text == "Writing\nthe file."  # assistant chunks joined by newline
    assert turn.tool_call_count == 1
    assert turn.tool_calls[0]["tool"] == "Write"
    assert turn.tool_calls[0]["state"]["status"] == "completed"
    assert turn.tokens == 0  # stream-json emits no usage summary
    assert turn.cost == 0.0
    assert turn.errors == []
    assert turn.error_summary is None


def test_occ_turn_error_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout="\n".join([_START, _ERROR]) + "\n", returncode=1)
    turn = driver.occ_run(Runner(), "box", "go", model="ollama/x")
    assert turn.ok is False
    assert len(turn.errors) == 1
    assert turn.error_summary == "provider unreachable: connection refused"


def test_occ_turn_max_turns_stop_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # A `stop` with an error-ish reason marks the turn degraded (hit the turn cap).
    _patch_capture(monkeypatch, stdout="\n".join([_START, _ASSIST_A, _STOP_MAXTURNS]) + "\n")
    turn = driver.occ_run(Runner(), "box", "go", model="ollama/x")
    assert turn.ok is False
    assert turn.error_summary == "stopped: max_turns"


def test_occ_turn_unparseable_stdout_yields_no_events(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout="not json at all")
    turn = driver.occ_run(Runner(), "box", "hi", model="ollama/x")
    assert turn.events == []
    assert turn.raw == "not json at all"
    assert turn.ok is False


def test_occ_run_defaults_to_30_max_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    # No explicit cap → occ's built-in default 30 (runaway-gate polite-stop unset).
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "go", model="ollama/x")
    assert "--max-turns 30" in _script(calls)


def test_occ_run_honors_explicit_max_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    # The runaway-gate resolved max_turns is threaded through as occ's --max-turns.
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.occ_run(Runner(), "box", "go", model="ollama/x", max_turns=55)
    assert "--max-turns 55" in _script(calls)
