"""Unit tests for the Phase-3 Codex driver — `codex_run` command construction (inline
`config.toml` heredoc + `codex exec --json` argv) and `CodexTurn` parsing of the NDJSON
event stream onto the shared `Turn` read surface. All subprocess calls are stubbed, so
these run without a Docker daemon (codex itself only runs in the VM). The event shapes
here were captured live from codex-cli 0.144.5 (Phase-0 spike, 2026-07-18) —
`.docs/codex-integration.md`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from book_em_danno.core import exec as exec_mod
from book_em_danno.core.exec import Runner
from danno_validator import driver
from danno_validator.driver import Turn


def _patch_capture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    returncode: int = 0,
    envs: list[dict[str, str] | None] | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        if envs is not None:
            envs.append(kw.get("env"))
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(exec_mod.subprocess, "run", fake_run)
    return calls


# --- faithful captures of `codex exec --json` NDJSON events (Phase-0 spike) -----------
_THREAD = json.dumps({"type": "thread.started", "thread_id": "th_abc123"})
_TURN_STARTED = json.dumps({"type": "turn.started"})
_REASONING = json.dumps(
    {"type": "item.completed", "item": {"type": "reasoning", "id": "r1", "text": "thinking"}}
)
_CMD_OK = json.dumps(
    {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "id": "c1",
            "command": "true",
            "aggregated_output": "",
            "exit_code": 0,
            "status": "completed",
        },
    }
)
_CMD_FAIL = json.dumps(
    {
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "id": "c2",
            "command": "false",
            "aggregated_output": "",
            "exit_code": 1,
            "status": "completed",
        },
    }
)
_AGENT_MSG = json.dumps(
    {"type": "item.completed", "item": {"type": "agent_message", "id": "m1", "text": "all done"}}
)
# A non-fatal `error` item (e.g. missing model metadata) — a warning, NOT a turn failure.
_ERROR_ITEM = json.dumps(
    {
        "type": "item.completed",
        "item": {"type": "error", "message": "Model metadata for `gpt-oss:20b` not found."},
    }
)
_USAGE = json.dumps(
    {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 11855,
            "cached_input_tokens": 0,
            "output_tokens": 146,
            "reasoning_output_tokens": 0,
        },
    }
)
_TURN_FAILED = json.dumps({"type": "turn.failed", "error": "provider stream error"})

_FULL_TURN = "\n".join([_THREAD, _TURN_STARTED, _REASONING, _CMD_OK, _AGENT_MSG, _USAGE]) + "\n"


def _script(calls: list[list[str]]) -> str:
    """The `bash -lc` script argument of the (single) captured exec."""
    argv = calls[0]
    assert argv[-3:-1] == ["bash", "-lc"]
    return argv[-1]


# --- codex_run command construction --------------------------------------------------


def test_codex_run_minimal_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.codex_run(Runner(), "box", "do the thing")
    script = _script(calls)
    # Inline config.toml written into a VM-local CODEX_HOME, then `codex exec --json`.
    assert "export CODEX_HOME=$HOME/.codex-danno" in script
    assert "codex exec --json -s danger-full-access --skip-git-repo-check" in script
    # Relay-free (Phase-0): base_url dials host Ollama's `/v1` directly (host.docker.internal,
    # NEVER localhost — that would bypass the egress proxy via no_proxy).
    assert "http://host.docker.internal:11434/v1" in script
    assert 'wire_api = "responses"' in script
    # stdin closed so codex never blocks reading additional input; prompt is the last arg.
    assert script.rstrip().endswith("'do the thing' </dev/null")


def test_codex_run_model_and_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.codex_run(Runner(), "box", "next", model="gpt-oss:20b", workspace="/repo")
    script = _script(calls)
    assert "-C /repo" in script  # codex exec cwd
    assert "-m gpt-oss:20b" in script  # bare tag within the configured provider


def test_codex_run_forwards_env_file_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Forwarded by NAME (`-e OPENAI_API_KEY`) with the value in the subprocess env —
    # sbx's `--env-file` is a no-op (issue #99) — secret never on the argv.
    env_file = tmp_path / "danno-env"
    env_file.write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    envs: list[dict[str, str] | None] = []
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN, envs=envs)
    driver.codex_run(Runner(), "box", "go", env_file=str(env_file))
    argv = calls[0]
    assert argv[:5] == ["docker", "sandbox", "exec", "-e", "OPENAI_API_KEY"]
    assert argv[5] == "box"
    assert "--env-file" not in argv
    assert "sk-secret" not in argv
    assert envs[0] is not None and envs[0]["OPENAI_API_KEY"] == "sk-secret"


def test_codex_run_capture_dials_recording_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # --capture points the Responses base_url at the host-side recording proxy
    # (host.docker.internal:<capture_port>/v1), still relay-free.
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.codex_run(Runner(), "box", "go", capture_port=54321)
    script = _script(calls)
    assert "http://host.docker.internal:54321/v1" in script
    assert "http://host.docker.internal:11434/v1" not in script


def test_codex_run_ignores_agent_session_and_max_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    # These exist only for TurnFn signature parity: codex `exec` is non-interactive, has no
    # `--agent`/session flag, and no polite-stop cap (the external watchdog is the bound).
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.codex_run(Runner(), "box", "go", agent="build", session="s1", max_turns=50)
    script = _script(calls)
    assert "--agent" not in script
    assert "--resume" not in script
    assert "--max-turns" not in script


# --- CodexTurn parsing ---------------------------------------------------------------


def test_codex_turn_parses_full_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout=_FULL_TURN)
    turn = driver.codex_run(Runner(), "box", "make a file")
    assert isinstance(turn, Turn)  # satisfies the shared read surface
    assert turn.ok is True
    assert turn.session_id == "th_abc123"  # thread.started thread_id
    assert turn.assistant_text == "all done"  # last agent_message text
    assert turn.tool_call_count == 1
    assert turn.tool_calls[0]["tool"] == "exec_command"
    assert turn.tool_calls[0]["callID"] == "c1"
    assert turn.tool_calls[0]["state"]["status"] == "completed"
    assert turn.tokens == 12001  # 11855 input + 146 output
    assert turn.cost == 0.0
    assert turn.errors == []
    assert turn.error_summary is None


def test_codex_turn_last_agent_message_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    first = json.dumps(
        {"type": "item.completed", "item": {"type": "agent_message", "id": "m1", "text": "draft"}}
    )
    stdout = "\n".join([_THREAD, first, _CMD_OK, _AGENT_MSG, _USAGE]) + "\n"
    _patch_capture(monkeypatch, stdout=stdout)
    turn = driver.codex_run(Runner(), "box", "go")
    assert turn.assistant_text == "all done"  # the later agent_message, not "draft"


def test_codex_turn_failed_exit_status_maps_to_error(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = "\n".join([_THREAD, _CMD_FAIL, _AGENT_MSG, _USAGE]) + "\n"
    _patch_capture(monkeypatch, stdout=stdout)
    turn = driver.codex_run(Runner(), "box", "go")
    # A command_execution with a non-zero exit_code is a tool error, not a turn failure.
    assert turn.tool_calls[0]["state"]["status"] == "error"
    assert turn.ok is True
    assert turn.errors == []


def test_codex_turn_error_item_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    # An `error` ITEM (missing model metadata warning) must NOT flip `ok` or count as a
    # failure — only `turn.failed`/non-zero exit is a real failure.
    stdout = "\n".join([_THREAD, _ERROR_ITEM, _CMD_OK, _AGENT_MSG, _USAGE]) + "\n"
    _patch_capture(monkeypatch, stdout=stdout)
    turn = driver.codex_run(Runner(), "box", "go")
    assert turn.ok is True
    assert turn.errors == []
    assert turn.error_summary is None


def test_codex_turn_failed_event(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = "\n".join([_THREAD, _TURN_FAILED]) + "\n"
    _patch_capture(monkeypatch, stdout=stdout, returncode=1)
    turn = driver.codex_run(Runner(), "box", "go")
    assert turn.ok is False
    assert len(turn.errors) == 1
    assert turn.error_summary == "provider stream error"


def test_codex_turn_unparseable_stdout_yields_no_events(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout="not json at all")
    turn = driver.codex_run(Runner(), "box", "hi")
    assert turn.events == []
    assert turn.raw == "not json at all"
    assert turn.ok is False
