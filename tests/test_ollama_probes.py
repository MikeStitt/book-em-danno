"""The Ollama capability probes must not read a transient network blip as a real negative
verdict: `verify_responds`/`tool_call_probe` retry (TRANSIENT) and only a persistent failure
escalates to ERROR and returns the negative answer; `ensure_model` retries a failed pull the
same way (policy §5, TRANSIENT→ERROR). All network/exec calls are stubbed."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from book_em_danno.commands import ollama
from book_em_danno.core.exec import CommandFailedError
from conftest import RecordingRunner


@pytest.fixture(autouse=True)
def _no_backoff_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    # retry_transient resolves time.sleep at call time; neutralize it so retries don't wait.
    monkeypatch.setattr("book_em_danno.core.exec.time.sleep", lambda _s: None)


def _json_resp(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


def test_verify_responds_retries_transient_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = {"n": 0}

    def flaky_urlopen(req: object, timeout: float = 0) -> io.BytesIO:
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.URLError("connection reset")
        return _json_resp({"response": "ok"})

    monkeypatch.setattr(ollama.urllib.request, "urlopen", flaky_urlopen)
    assert ollama.verify_responds("qwen") is True
    assert calls["n"] == 2  # the blip was retried, not read as a failure
    assert "[TRANSIENT]" in capsys.readouterr().err


def test_verify_responds_persistent_failure_escalates_and_returns_false(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def dead_urlopen(req: object, timeout: float = 0) -> io.BytesIO:
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(ollama.urllib.request, "urlopen", dead_urlopen)
    assert ollama.verify_responds("qwen") is False
    assert "[ERROR]" in capsys.readouterr().err  # not a silent False


def test_verify_responds_negative_body_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def urlopen_missing_field(req: object, timeout: float = 0) -> io.BytesIO:
        calls["n"] += 1
        return _json_resp({"done": True})  # a real 200 with no `response` field

    monkeypatch.setattr(ollama.urllib.request, "urlopen", urlopen_missing_field)
    assert ollama.verify_responds("qwen") is False
    assert calls["n"] == 1  # a parsed negative answer is definitive, not a blip


def test_tool_call_probe_retries_transient_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def flaky_urlopen(req: object, timeout: float = 0) -> io.BytesIO:
        calls["n"] += 1
        if calls["n"] < 2:
            raise OSError("blip")
        return _json_resp({"message": {"tool_calls": [{"function": {"name": "get_weather"}}]}})

    monkeypatch.setattr(ollama.urllib.request, "urlopen", flaky_urlopen)
    assert ollama.tool_call_probe("qwen") is True
    assert calls["n"] == 2


def test_ensure_model_retries_failed_pull_then_succeeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = {"n": 0}

    class _FlakyRunner(RecordingRunner):
        def advise(self, cmd: list[str], why: str, **kw: object) -> list[str]:  # type: ignore[override]
            calls["n"] += 1
            if calls["n"] < 2:
                raise CommandFailedError("registry blip")
            return cmd

    assert ollama.ensure_model(_FlakyRunner(), "qwen") == ["ollama", "pull", "qwen"]
    assert calls["n"] == 2
    assert "[TRANSIENT]" in capsys.readouterr().err


def test_ensure_model_persistent_pull_failure_escalates_and_raises(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _DeadRunner(RecordingRunner):
        def advise(self, cmd: list[str], why: str, **kw: object) -> list[str]:  # type: ignore[override]
            raise CommandFailedError("model not found")

    with pytest.raises(CommandFailedError, match="model not found"):
        ollama.ensure_model(_DeadRunner(), "ghost")
    assert "[ERROR]" in capsys.readouterr().err


def test_ensure_model_advise_only_does_not_retry() -> None:
    # A non-apply run never executes, so advise returns on the first call — no retry loop.
    r = RecordingRunner()
    assert ollama.ensure_model(r, "qwen") == ["ollama", "pull", "qwen"]
    assert r.commands == [["ollama", "pull", "qwen"]]  # recorded exactly once


def test_installed_tags_unparseable_body_warns_and_is_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Ollama answered but with non-JSON — that is malformed upstream, NOT "no models pulled".
    # It must WARN (greppable) before falling back to empty, else a redundant pull is advised
    # as if deliberate (policy §5, WARNING).
    monkeypatch.setattr(
        ollama.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"<html>not json</html>")
    )
    assert ollama.installed_tags() == set()
    err = " ".join(capsys.readouterr().err.split())
    assert "[WARNING]" in err and "unparseable JSON" in err


def test_installed_tags_unreachable_is_silent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # An unreachable Ollama is a legitimately-absent tag list (caller advises the pull) — the
    # empty set is expected, NOT an anomaly, so it stays silent (distinct from the corrupt case).
    def dead_urlopen(*a: object, **k: object) -> io.BytesIO:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ollama.urllib.request, "urlopen", dead_urlopen)
    assert ollama.installed_tags() == set()
    assert capsys.readouterr().err == ""


def test_announce_lan_exposure_is_a_real_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A public-interface Ollama bind is a real security anomaly, so it emits a genuine WARNING —
    # not INFO carrying a hand-rolled `[yellow]WARN[/yellow]` tag the formatter would double-tag
    # and `-q` could not suppress independently (policy §5).
    monkeypatch.setattr(ollama, "lan_exposure_warning", lambda **k: "Host Ollama is public")
    ollama.announce_lan_exposure()
    err = " ".join(capsys.readouterr().err.split())
    assert "[WARNING]" in err and "Host Ollama is public" in err
    assert "[yellow]WARN[/yellow]" not in err  # no hand-rolled tag leaks through
