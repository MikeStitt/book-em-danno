"""Unit tests for the sandbox egress reachability artifact (`capture.egress`, issue #101).

Hermetic: a stub Runner returns canned `sbx policy log --json` output, so nothing shells out
to a real daemon. The docker backend returns `None` WITHOUT any exec (the fast-suite safety
net under the `DANNO_SANDBOX_CLI=docker` pin); the sbx probe-failure paths do exec and then
WARN before returning `None` — absent vs failed are distinct facts (policy §5)."""

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

    def __init__(
        self, *, stdout: str, returncode: int = 0, raises: bool = False, stderr: str = ""
    ) -> None:
        self._stdout = stdout
        self._returncode = returncode
        self._raises = raises
        self._stderr = stderr
        self.commands: list[list[str]] = []

    def capture(self, cmd: list[str], **kw: object) -> CaptureResult:
        self.commands.append(cmd)
        if self._raises:
            raise OSError("sandbox gone")
        return CaptureResult(
            cmd=cmd, returncode=self._returncode, stdout=self._stdout, stderr=self._stderr
        )


def _sbx(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "sbx")


def test_snapshot_execs_policy_log_and_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _sbx(monkeypatch)
    runner = _LogRunner(stdout=json.dumps(_LOG))
    got = egress.snapshot_egress_log(runner, "box")  # type: ignore[arg-type]
    assert got == _LOG
    assert runner.commands == [["sbx", "policy", "log", "box", "--type", "network", "--json"]]


def test_snapshot_docker_backend_returns_none_silently_without_exec(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # docker has no host-side egress audit → None, and crucially NO exec (the fast-suite safety
    # net: wiring that reaches this under the DANNO_SANDBOX_CLI=docker pin never shells out).
    # This is legitimately-ABSENT, distinct from a probe failure, so it stays SILENT (policy §5).
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "docker")
    runner = _LogRunner(stdout="should-not-be-read")
    assert egress.snapshot_egress_log(runner, "box") is None  # type: ignore[arg-type]
    assert runner.commands == []
    assert capsys.readouterr().err == ""  # absent ≠ failed: no warning


def test_snapshot_probe_failures_warn_before_returning_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Under sbx a probe that could-not-run / exited-non-zero / returned unparseable or
    # non-object output is a genuine anomaly during a --capture run (the audit the operator
    # asked for silently went missing) — each must WARN (greppable) before returning None,
    # NOT swallow like the docker-backend absent case (policy §5). RichHandler wraps, so
    # normalize whitespace before substring-matching.
    _sbx(monkeypatch)

    def _warns(runner: egress.Runner, needle: str) -> None:
        assert egress.snapshot_egress_log(runner, "box") is None
        err = " ".join(capsys.readouterr().err.split())
        assert "[WARNING]" in err and "box" in err and needle in err

    # non-zero exit → surfaces the exit code + stderr detail
    _warns(_LogRunner(stdout="", returncode=1, stderr="daemon down"), "daemon down")  # type: ignore[arg-type]
    # exec cannot even launch (OSError)
    _warns(_LogRunner(stdout="", raises=True), "could not run")  # type: ignore[arg-type]
    # garbage JSON
    _warns(_LogRunner(stdout="not json"), "unparseable JSON")  # type: ignore[arg-type]
    # well-formed JSON that is not an object
    _warns(_LogRunner(stdout="[1,2]"), "not a JSON object")  # type: ignore[arg-type]


def test_warn_on_blocked_names_hosts(capsys: pytest.CaptureFixture[str]) -> None:
    egress.warn_on_blocked("box", _LOG)
    err = capsys.readouterr().err  # log_warn → rich Console → stderr (the log channel)
    assert "BLOCKED" in err and "en.wikipedia.org:443" in err and "box" in err


def test_warn_on_blocked_silent_when_clean(capsys: pytest.CaptureFixture[str]) -> None:
    egress.warn_on_blocked("box", {"allowed_hosts": [], "blocked_hosts": []})
    assert capsys.readouterr().err == ""


def test_warn_on_blocked_silent_when_key_absent(capsys: pytest.CaptureFixture[str]) -> None:
    # No blocked_hosts key at all = legitimately no blocks recorded — stays silent (absent case).
    egress.warn_on_blocked("box", {"allowed_hosts": []})
    assert capsys.readouterr().err == ""


def test_warn_on_blocked_warns_on_malformed_non_list(capsys: pytest.CaptureFixture[str]) -> None:
    # A non-list blocked_hosts would otherwise read as "no blocks" and hide a real egress block —
    # the malformed shape must WARN, not be swallowed (policy §5).
    egress.warn_on_blocked("box", {"blocked_hosts": "oops-not-a-list"})
    err = " ".join(capsys.readouterr().err.split())
    assert "[WARNING]" in err and "non-list blocked_hosts" in err and "box" in err


def test_write_egress_artifact_warns_on_unwritable_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A failed durable write of the companion audit must not raise (it would crash an otherwise-
    # good run / mask a teardown exception) — it WARNs and returns None (policy §5). Point the
    # capture dir under a FILE so mkdir raises deterministically.
    (tmp_path / "afile").write_text("i am a file, not a dir")
    out = egress.write_egress_artifact(
        {"box": _LOG}, capture_dir=tmp_path / "afile" / "cap", run_start="t"
    )
    assert out is None
    err = " ".join(capsys.readouterr().err.split())
    assert "[WARNING]" in err and "could not write the reachability log" in err


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
