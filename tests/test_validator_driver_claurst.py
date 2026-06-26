"""Unit tests for the M1 Claurst driver — `claurst_run` command construction
(relay-wrapped `bash -lc`) and `ClaurstTurn` parsing of `claurst -p --output-format
stream-json` JSONL onto the shared `Turn` read surface. All subprocess calls are
stubbed, so these run without a Docker daemon (claurst itself only runs in the VM).
The event shapes here were captured live from claurst 0.1.5 (M0 spike, 2026-06-23)."""

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


# Faithful captures of claurst `--output-format stream-json` events (M0 spike): a
# file-write task emits a tool_start, sub-word text deltas, and a terminal result.
_TOOL = '{"tool":"Write","type":"tool_start"}'
_DELTA_HEL = '{"text":"HEL","type":"text_delta"}'
_DELTA_LO = '{"text":"LO","type":"text_delta"}'
_RESULT = '{"cost_usd":0.0,"type":"result","usage":{"input_tokens":12,"output_tokens":8}}'
_RESULT_TEXT = '{"cost_usd":0.5,"type":"result","result":"All done.","usage":{}}'
_ERROR = '{"error":"API error: [ollama] Model not found: unknown","type":"error"}'

_FULL_TURN = "\n".join([_TOOL, _DELTA_HEL, _DELTA_LO, _RESULT]) + "\n"


def _script(calls: list[list[str]]) -> str:
    """The `bash -lc` script argument of the (single) captured exec."""
    argv = calls[0]
    assert argv[:6] == ["docker", "sandbox", "exec", "box", "bash", "-lc"]
    return argv[6]


def test_claurst_run_minimal_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claurst_run(Runner(), "box", "do the thing")
    script = _script(calls)
    assert "OLLAMA_HOST=http://127.0.0.1:11434 claurst -p --output-format stream-json" in script
    assert script.rstrip().endswith("'do the thing'")  # prompt is shell-quoted, last arg


def test_claurst_run_wraps_with_ollama_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    # The relay is written + started + readiness-probed + trap-killed inside the exec.
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claurst_run(Runner(), "box", "go")
    script = _script(calls)
    assert "mktemp /tmp/danno-relay-" in script
    assert "ThreadingHTTPServer" in script  # the relay source is heredoc'd in
    assert "http://127.0.0.1:11434/api/tags" in script  # readiness probe
    assert "trap 'kill $DANNO_RELAY_PID" in script


def test_claurst_run_model_skip_permissions_workspace_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claurst_run(
        Runner(),
        "box",
        "next",
        model="ollama/llama3.2:latest",
        skip_permissions=True,
        workspace="/repo",
        session="sess-1",
    )
    script = _script(calls)
    assert "-m ollama/llama3.2:latest" in script
    assert "--dangerously-skip-permissions" in script
    assert "--cwd /repo" in script
    assert "--resume sess-1" in script


def test_claurst_run_passes_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claurst_run(Runner(), "box", "go", env_file="/tmp/danno-env-xyz")
    argv = calls[0]
    assert argv[:3] == ["docker", "sandbox", "exec"]
    assert argv[3] == "--env-file"
    assert argv[4] == "/tmp/danno-env-xyz"
    assert argv[5] == "box"


def test_claurst_run_ignores_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    # agent exists only for TurnFn signature parity; claurst has no --agent.
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claurst_run(Runner(), "box", "go", agent="build")
    assert "--agent" not in _script(calls)


def test_claurst_run_default_relay_upstream_is_real_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without --capture the relay forwards to real host Ollama (port 11434).
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claurst_run(Runner(), "box", "go")
    assert "DANNO_RELAY_UPSTREAM_PORT=11434 python3" in _script(calls)


def test_claurst_run_capture_port_redirects_relay_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # --capture points the relay at a host-side recording proxy port instead.
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claurst_run(Runner(), "box", "go", capture_port=54321)
    script = _script(calls)
    assert "DANNO_RELAY_UPSTREAM_PORT=54321 python3" in script
    assert 'python3 "$RELAY_PY" 11434' in script  # relay still LISTENS on 11434


def test_relay_source_reads_upstream_port_from_env() -> None:
    # The relay builds its upstream from DANNO_RELAY_UPSTREAM_PORT (defaulting to 11434),
    # so --capture redirects it without re-templating the heredoc'd relay source.
    assert 'os.environ.get("DANNO_RELAY_UPSTREAM_PORT", "11434")' in driver._OLLAMA_RELAY_SOURCE


def test_claurst_turn_parses_full_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout=_FULL_TURN)
    turn = driver.claurst_run(Runner(), "box", "make a file")
    assert isinstance(turn, Turn)  # satisfies the shared read surface
    assert turn.ok is True
    assert turn.session_id is None  # claurst exposes no session id
    assert turn.assistant_text == "HELLO"  # deltas concatenated without separators
    assert turn.tool_call_count == 1
    assert turn.tool_calls[0]["tool"] == "Write"
    assert turn.tool_calls[0]["state"]["status"] == "completed"
    assert turn.tokens == 20  # usage 12 + 8
    assert turn.cost == 0.0
    assert turn.errors == []
    assert turn.error_summary is None


def test_claurst_turn_assistant_text_falls_back_to_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A turn with no text deltas still yields text via the result event's `result`.
    _patch_capture(monkeypatch, stdout="\n".join([_TOOL, _RESULT_TEXT]) + "\n")
    turn = driver.claurst_run(Runner(), "box", "go")
    assert turn.assistant_text == "All done."
    assert turn.cost == 0.5


def test_claurst_turn_error_event(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout=_ERROR + "\n", returncode=1)
    turn = driver.claurst_run(Runner(), "box", "go")
    assert turn.ok is False
    assert len(turn.errors) == 1
    assert turn.error_summary == "API error: [ollama] Model not found: unknown"


def test_claurst_turn_unparseable_stdout_yields_no_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_capture(monkeypatch, stdout="not json at all")
    turn = driver.claurst_run(Runner(), "box", "hi")
    assert turn.events == []
    assert turn.raw == "not json at all"
    assert turn.ok is False
