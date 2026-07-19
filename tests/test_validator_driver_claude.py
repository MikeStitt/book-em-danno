"""Unit tests for the M5 Claude Code driver — `claude_run` command construction
and `ClaudeTurn` parsing of `claude -p --output-format stream-json` JSONL onto the
shared `Turn` read surface. All subprocess calls are stubbed, so these run without
a Docker daemon (claude itself only ever runs in the VM). The stream-json schema
here is the shape the parser targets; pin it live before relying on it."""

from __future__ import annotations

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


# A trimmed but faithful capture of Claude Code `--output-format stream-json`
# stdout: a system init, an assistant text turn, an assistant tool_use, the
# tool_result, and the final result event carrying totals.
_SYSTEM = (
    '{"type":"system","subtype":"init","cwd":"/repo","session_id":"sess-abc",'
    '"model":"claude-opus-4-8[1m]"}'
)
_ASSISTANT_TEXT = (
    '{"type":"assistant","message":{"role":"assistant","content":'
    '[{"type":"text","text":"I will create the file."}],'
    '"usage":{"input_tokens":10,"output_tokens":5}},"session_id":"sess-abc"}'
)
_ASSISTANT_TOOL = (
    '{"type":"assistant","message":{"role":"assistant","content":'
    '[{"type":"tool_use","id":"toolu_1","name":"Write","input":{"file_path":"foo.txt"}}],'
    '"usage":{"input_tokens":12,"output_tokens":20}},"session_id":"sess-abc"}'
)
_TOOL_RESULT_OK = (
    '{"type":"user","message":{"role":"user","content":'
    '[{"type":"tool_result","tool_use_id":"toolu_1","content":"ok","is_error":false}]},'
    '"session_id":"sess-abc"}'
)
_RESULT_OK = (
    '{"type":"result","subtype":"success","is_error":false,"num_turns":3,'
    '"result":"Done — created foo.txt.","session_id":"sess-abc","total_cost_usd":0.0123,'
    '"usage":{"input_tokens":22,"output_tokens":25}}'
)

_FULL_TURN = (
    "\n".join([_SYSTEM, _ASSISTANT_TEXT, _ASSISTANT_TOOL, _TOOL_RESULT_OK, _RESULT_OK]) + "\n"
)


def test_claude_run_minimal_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claude_run(Runner(), "box", "do the thing")
    assert calls == [
        [
            "docker",
            "sandbox",
            "exec",
            "box",
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "do the thing",
        ]
    ]


def test_claude_run_session_skip_permissions_and_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claude_run(
        Runner(), "box", "next", session="sess-1", skip_permissions=True, workspace="/repo"
    )
    assert calls == [
        [
            "docker",
            "sandbox",
            "exec",
            "-w",
            "/repo",
            "box",
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--resume",
            "sess-1",
            "next",
        ]
    ]


def test_claude_run_forwards_env_file_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # claude needs its auth token in the exec env; a bare exec inherits none. It is
    # forwarded by NAME (`-e CLAUDE_CODE_OAUTH_TOKEN`) with the value in the subprocess env —
    # sbx's `--env-file` is a no-op (issue #99) — and the secret never rides the argv.
    env_file = tmp_path / "danno-env"
    env_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=tok-secret\n", encoding="utf-8")
    envs: list[dict[str, str] | None] = []
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN, envs=envs)
    driver.claude_run(Runner(), "box", "go", workspace="/repo", env_file=str(env_file))
    argv = calls[0]
    assert argv[:7] == [
        "docker",
        "sandbox",
        "exec",
        "-w",
        "/repo",
        "-e",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ]
    assert argv[7] == "box"
    assert "--env-file" not in argv
    assert "tok-secret" not in argv
    assert envs[0] is not None and envs[0]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-secret"


def test_claude_run_pins_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # model is controlled like opencode's -m, via claude's --model.
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claude_run(Runner(), "box", "go", model="opus")
    argv = calls[0]
    assert argv[argv.index("--model") + 1] == "opus"
    assert "-m" not in argv  # claude uses --model, not opencode's -m


def test_claude_run_omits_model_when_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claude_run(Runner(), "box", "go")
    assert "--model" not in calls[0]  # no model → claude's install default


def test_claude_run_ignores_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    # agent exists only for TurnFn signature parity; claude has no --agent.
    calls = _patch_capture(monkeypatch, stdout=_FULL_TURN)
    driver.claude_run(Runner(), "box", "go", agent="build")
    assert "--agent" not in calls[0]


def test_claude_turn_reports_resolved_model(monkeypatch: pytest.MonkeyPatch) -> None:
    # The actual model is tracked from the system init event (even if unpinned).
    _patch_capture(monkeypatch, stdout=_FULL_TURN)
    turn = driver.claude_run(Runner(), "box", "go")
    assert turn.model == "claude-opus-4-8[1m]"


def test_claude_turn_parses_full_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout=_FULL_TURN)
    turn = driver.claude_run(Runner(), "box", "make a file")
    assert isinstance(turn, Turn)  # satisfies the shared read surface
    assert turn.ok is True
    assert turn.session_id == "sess-abc"
    assert turn.assistant_text == "I will create the file."
    assert turn.tool_call_count == 1
    assert turn.tool_calls[0]["tool"] == "Write"
    assert turn.tool_calls[0]["state"]["status"] == "completed"
    assert turn.tokens == 47  # result usage 22 + 25
    assert turn.cost == pytest.approx(0.0123)
    assert turn.errors == []
    assert turn.error_summary is None


def test_claude_turn_assistant_text_falls_back_to_result(monkeypatch: pytest.MonkeyPatch) -> None:
    # A turn that only acts (no streamed text block) still yields text via `result`.
    stdout = "\n".join([_SYSTEM, _ASSISTANT_TOOL, _TOOL_RESULT_OK, _RESULT_OK]) + "\n"
    _patch_capture(monkeypatch, stdout=stdout)
    turn = driver.claude_run(Runner(), "box", "go")
    assert turn.assistant_text == "Done — created foo.txt."


def test_claude_turn_marks_tool_error_from_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    tool_result_err = (
        '{"type":"user","message":{"role":"user","content":'
        '[{"type":"tool_result","tool_use_id":"toolu_1","content":"boom","is_error":true}]},'
        '"session_id":"sess-abc"}'
    )
    stdout = "\n".join([_SYSTEM, _ASSISTANT_TOOL, tool_result_err, _RESULT_OK]) + "\n"
    _patch_capture(monkeypatch, stdout=stdout)
    turn = driver.claude_run(Runner(), "box", "go")
    assert turn.tool_calls[0]["state"]["status"] == "error"


def test_claude_turn_failed_result_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    result_err = (
        '{"type":"result","subtype":"error_during_execution","is_error":true,'
        '"result":"context limit exceeded","session_id":"s","total_cost_usd":0,"usage":{}}'
    )
    stdout = "\n".join([_SYSTEM, result_err]) + "\n"
    _patch_capture(monkeypatch, stdout=stdout, returncode=1)
    turn = driver.claude_run(Runner(), "box", "go")
    assert turn.ok is False
    assert len(turn.errors) == 1
    assert turn.error_summary == "context limit exceeded"


def test_claude_turn_unparseable_stdout_yields_no_events(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout="not json at all")
    turn = driver.claude_run(Runner(), "box", "hi")
    assert turn.events == []
    assert turn.raw == "not json at all"
    assert turn.ok is False
