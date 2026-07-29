"""Unit tests for the sandbox egress reachability artifact (`capture.egress`, issue #101).

Hermetic: a stub Runner returns canned `sbx policy log --json` output, so nothing shells out
to a real daemon. Docker-backend and probe-failure paths return `None` WITHOUT any exec — that
is also what keeps the wiring safe under the fast suite's `DANNO_SANDBOX_CLI=docker` pin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from book_em_danno.capture import egress
from book_em_danno.core.exec import CaptureResult

_LOG = {
    "allowed_hosts": [
        {"host": "localhost:11434", "vm_name": "box", "rule": "domain-allowed", "count_since": 3}
    ],
    "blocked_hosts": [{"host": "en.wikipedia.org:443", "vm_name": "box", "reason": "default deny"}],
}


class _LogRunner:
    """Minimal Runner stub: records exec'd commands, returns canned `policy log` JSON."""

    def __init__(self, *, stdout: str, returncode: int = 0, raises: bool = False) -> None:
        self._stdout = stdout
        self._returncode = returncode
        self._raises = raises
        self.commands: list[list[str]] = []

    def capture(self, cmd: list[str], **kw: object) -> CaptureResult:
        self.commands.append(cmd)
        if self._raises:
            raise OSError("sandbox gone")
        return CaptureResult(cmd=cmd, returncode=self._returncode, stdout=self._stdout, stderr="")


def _sbx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")


def test_snapshot_execs_policy_log_and_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _sbx(monkeypatch)
    runner = _LogRunner(stdout=json.dumps(_LOG))
    got = egress.snapshot_egress_log(runner, "box")  # type: ignore[arg-type]
    assert got == _LOG
    assert runner.commands == [["sbx", "policy", "log", "box", "--type", "network", "--json"]]


def test_snapshot_docker_backend_returns_none_without_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    # docker has no host-side egress audit → None, and crucially NO exec (the fast-suite safety
    # net: wiring that reaches this under the DANNO_SANDBOX_CLI=docker pin never shells out).
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "docker")
    runner = _LogRunner(stdout="should-not-be-read")
    assert egress.snapshot_egress_log(runner, "box") is None  # type: ignore[arg-type]
    assert runner.commands == []


def test_snapshot_tolerates_failure_and_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _sbx(monkeypatch)
    # non-zero exit → None, never a raise
    assert egress.snapshot_egress_log(_LogRunner(stdout="{}", returncode=1), "box") is None  # type: ignore[arg-type]
    # exec cannot even launch → None, never a raise
    assert egress.snapshot_egress_log(_LogRunner(stdout="", raises=True), "box") is None  # type: ignore[arg-type]
    # garbage / non-object JSON → None
    assert egress.snapshot_egress_log(_LogRunner(stdout="not json"), "box") is None  # type: ignore[arg-type]
    assert egress.snapshot_egress_log(_LogRunner(stdout="[1,2]"), "box") is None  # type: ignore[arg-type]


def test_warn_on_blocked_names_hosts(capsys: pytest.CaptureFixture[str]) -> None:
    egress.warn_on_blocked("box", _LOG)
    err = capsys.readouterr().err  # log_warn → rich Console → stderr (the log channel)
    assert "BLOCKED" in err and "en.wikipedia.org:443" in err and "box" in err


def test_warn_on_blocked_silent_when_clean(capsys: pytest.CaptureFixture[str]) -> None:
    egress.warn_on_blocked("box", {"allowed_hosts": [], "blocked_hosts": []})
    assert capsys.readouterr().err == ""


def test_record_egress_writes_uniform_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _sbx(monkeypatch)
    runner = _LogRunner(stdout=json.dumps(_LOG))
    path = egress.record_egress(
        runner,  # type: ignore[arg-type]
        ["box", "box"],  # de-duplicated
        capture_dir=tmp_path / "captures",
        run_start="2026-07-29T00:00:00Z",
    )
    assert path == tmp_path / "captures" / "egress.json"
    payload = json.loads(path.read_text())
    assert payload["run_start"] == "2026-07-29T00:00:00Z"
    assert payload["note"] == egress.EGRESS_NOTE
    assert list(payload["sandboxes"]) == ["box"]
    assert payload["sandboxes"]["box"] == _LOG
    # exactly one probe despite the duplicate name
    assert len(runner.commands) == 1


def test_record_egress_writes_nothing_when_no_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # docker backend → every probe is None → no file written (no empty artifact left behind).
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "docker")
    out = tmp_path / "captures"
    assert (
        egress.record_egress(
            _LogRunner(stdout="x"),  # type: ignore[arg-type]
            ["box"],
            capture_dir=out,
            run_start="t",
        )
        is None
    )
    assert not (out / "egress.json").exists()


def test_accumulate_egress_merges_multiple_sandboxes(monkeypatch: pytest.MonkeyPatch) -> None:
    _sbx(monkeypatch)
    acc: dict[str, dict] = {}
    egress.accumulate_egress(_LogRunner(stdout=json.dumps(_LOG)), "aider", acc)  # type: ignore[arg-type]
    egress.accumulate_egress(_LogRunner(stdout=json.dumps({"allowed_hosts": []})), "swe", acc)  # type: ignore[arg-type]
    egress.accumulate_egress(_LogRunner(stdout="", returncode=1), "dead", acc)  # type: ignore[arg-type]
    assert list(acc) == ["aider", "swe"]  # the failed probe adds nothing
