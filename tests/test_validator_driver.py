"""M0 unit tests for danno_validator.driver — command construction, lenient JSON
parsing, and the destructive-reset marker guard. All subprocess calls are stubbed,
so these run without a Docker daemon (the AUT itself only ever runs in the VM)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from book_em_danno.core import exec as exec_mod
from book_em_danno.core.exec import CommandFailedError, Runner
from danno_validator import driver


def _patch_capture(
    monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", returncode: int = 0
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    monkeypatch.setattr(exec_mod.subprocess, "run", fake_run)
    return calls


def test_capture_exec_builds_non_tty_bash_lc(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout="ok")
    res = driver.capture_exec(Runner(), "danno-box", "ls -la")
    assert calls == [["docker", "sandbox", "exec", "danno-box", "bash", "-lc", "ls -la"]]
    assert res.stdout == "ok"


# A trimmed but faithful capture of opencode 1.17.7 `--format json` stdout: JSONL
# events (one per line), the schema the driver/oracle read. Pinned live at M1.
_TEXT_TURN = (
    '{"type":"step_start","sessionID":"ses_1","part":{"type":"step-start"}}\n'
    '{"type":"text","sessionID":"ses_1","part":{"type":"text","text":"I will create the file."}}\n'
    '{"type":"step_finish","sessionID":"ses_1",'
    '"part":{"type":"step-finish","reason":"stop","tokens":{"total":3516},"cost":0}}\n'
)
_TOOL_TURN = (
    '{"type":"step_start","sessionID":"ses_2","part":{"type":"step-start"}}\n'
    '{"type":"tool","sessionID":"ses_2","part":{"type":"tool","tool":"write",'
    '"callID":"call_x","state":{"status":"completed","output":"Wrote file successfully."}}}\n'
    '{"type":"text","sessionID":"ses_2","part":{"type":"text","text":"done"}}\n'
    '{"type":"step_finish","sessionID":"ses_2",'
    '"part":{"type":"step-finish","reason":"stop","tokens":{"total":200},"cost":0}}\n'
)


def test_opencode_run_minimal_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_TEXT_TURN)
    driver.opencode_run(Runner(), "box", "do the thing")
    assert calls == [
        ["docker", "sandbox", "exec", "box", "opencode", "run", "--format", "json", "do the thing"]
    ]


def test_opencode_run_with_session_agent_and_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_TOOL_TURN)
    driver.opencode_run(
        Runner(), "box", "next turn", session="sess-1", agent="build", workspace="/repo"
    )
    assert calls == [
        [
            "docker",
            "sandbox",
            "exec",
            "-w",
            "/repo",
            "box",
            "opencode",
            "run",
            "--format",
            "json",
            "--agent",
            "build",
            driver.OPENCODE_SESSION_FLAG,
            "sess-1",
            "next turn",
        ]
    ]


def test_opencode_run_model_and_skip_permissions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_capture(monkeypatch, stdout=_TOOL_TURN)
    driver.opencode_run(
        Runner(), "box", "go", agent="build", model="ollama/gemma3:27b", skip_permissions=True
    )
    assert calls == [
        [
            "docker",
            "sandbox",
            "exec",
            "box",
            "opencode",
            "run",
            "--format",
            "json",
            "--agent",
            "build",
            "-m",
            "ollama/gemma3:27b",
            "--dangerously-skip-permissions",
            "go",
        ]
    ]


def test_opencode_run_parses_jsonl_text_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout=_TEXT_TURN)
    turn = driver.opencode_run(Runner(), "box", "hi")
    assert turn.ok is True
    assert turn.session_id == "ses_1"
    assert turn.assistant_text == "I will create the file."
    assert turn.tool_call_count == 0
    assert turn.finish_reason == "stop"
    assert turn.tokens == 3516
    assert turn.cost == 0.0


def test_opencode_run_reads_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout=_TOOL_TURN)
    turn = driver.opencode_run(Runner(), "box", "make a file")
    assert turn.tool_call_count == 1
    assert turn.tool_calls[0]["tool"] == "write"
    assert turn.tool_calls[0]["state"]["status"] == "completed"
    assert turn.assistant_text == "done"


def test_tool_use_event_counts_as_a_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Some turns emit only `tool_use` (streamed call) rather than `tool` (result).
    stdout = (
        '{"type":"step_start","sessionID":"s","part":{}}\n'
        '{"type":"tool_use","sessionID":"s","part":{"type":"tool","tool":"write","callID":"c1"}}\n'
        '{"type":"text","sessionID":"s","part":{"type":"text","text":"done"}}\n'
        '{"type":"step_finish","sessionID":"s","part":{"reason":"stop"}}\n'
    )
    _patch_capture(monkeypatch, stdout=stdout)
    turn = driver.opencode_run(Runner(), "box", "go")
    assert turn.tool_call_count == 1


def test_tool_and_tool_use_same_call_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    # One call can surface as both a `tool_use` and a `tool` event; dedupe by callID.
    stdout = (
        '{"type":"tool_use","sessionID":"s","part":{"tool":"write","callID":"c1"}}\n'
        '{"type":"tool","sessionID":"s","part":{"tool":"write","callID":"c1",'
        '"state":{"status":"completed"}}}\n'
    )
    _patch_capture(monkeypatch, stdout=stdout)
    turn = driver.opencode_run(Runner(), "box", "go")
    assert turn.tool_call_count == 1


def test_parse_events_drops_non_json_log_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    # `--format json` interleaves human log blocks with the JSONL stream; the
    # error is still recoverable from its one-line event.
    stdout = (
        "[16:27:57.634] ERROR (#10941): failed {\n"
        '  ref: "err_8d8e41c4",\n'
        "}\n"
        '{"type":"error","sessionID":"ses_9","error":{"_tag":"ProviderModelNotFoundError"}}\n'
    )
    _patch_capture(monkeypatch, stdout=stdout, returncode=1)
    turn = driver.opencode_run(Runner(), "box", "hi")
    assert len(turn.events) == 1  # the multi-line log block was dropped
    assert len(turn.errors) == 1
    assert turn.ok is False


def test_error_summary_surfaces_api_message(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = (
        '{"type":"error","sessionID":"s","error":{"name":"APIError",'
        '"data":{"message":"gemma3:27b does not support tools"}}}\n'
    )
    _patch_capture(monkeypatch, stdout=stdout, returncode=1)
    turn = driver.opencode_run(Runner(), "box", "go")
    assert turn.error_summary == "APIError: gemma3:27b does not support tools"


def test_error_summary_falls_back_to_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = '{"type":"error","sessionID":"s","error":{"_tag":"ProviderModelNotFoundError"}}\n'
    _patch_capture(monkeypatch, stdout=stdout, returncode=1)
    turn = driver.opencode_run(Runner(), "box", "go")
    assert turn.error_summary == "ProviderModelNotFoundError"


def test_opencode_run_unparseable_stdout_yields_no_events(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout="not json at all")
    turn = driver.opencode_run(Runner(), "box", "hi")
    assert turn.events == []
    assert turn.raw == "not json at all"
    assert turn.ok is False  # zero exit but nothing parsed


def test_opencode_run_nonzero_exit_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_capture(monkeypatch, stdout=_TEXT_TURN, returncode=1)
    turn = driver.opencode_run(Runner(), "box", "hi")
    assert turn.result.returncode == 1
    assert turn.ok is False  # non-zero exit


def test_seed_workspace_creates_dir_and_marker(tmp_path: Path) -> None:
    ws = driver.seed_workspace(tmp_path / "run-1")
    assert ws.is_dir()
    assert (ws / driver.WORKSPACE_MARKER).is_file()
    assert driver.is_validator_workspace(ws) is True


def test_seed_workspace_is_idempotent(tmp_path: Path) -> None:
    driver.seed_workspace(tmp_path / "ws")
    driver.seed_workspace(tmp_path / "ws")  # re-run must not raise
    assert driver.is_validator_workspace(tmp_path / "ws") is True


def test_reset_workspace_refuses_unmarked_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_capture(monkeypatch)
    with pytest.raises(CommandFailedError, match="missing the .danno-validator-workspace"):
        driver.reset_workspace(Runner(), "box", tmp_path)  # no marker → refuse
    assert calls == []  # guarded before any subprocess ran


def test_reset_workspace_runs_guarded_git_clean_reset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws = driver.seed_workspace(tmp_path / "ws")
    calls = _patch_capture(monkeypatch)
    driver.reset_workspace(Runner(), "box", ws)
    assert len(calls) == 1
    shell = calls[0]
    assert shell[:6] == ["docker", "sandbox", "exec", "box", "bash", "-lc"]
    command = shell[6]
    # git clean excludes the marker so the guard keeps holding across resets.
    assert "git clean -fdx -e" in command
    assert driver.WORKSPACE_MARKER in command
    assert "git reset --hard" in command
    assert str(ws) in command
