"""V4 — config-is-code capture lifecycle (GV3, Tier B).

The `--no-save-captures` contract in-sandbox: the always-on gate proxy writes NOTHING to disk
— no capture dir, no JSONL, no transcript — on a completed run OR an aborted one (SIGINT
mid-run). This supersedes the old F4 temp-root cleanup: there is no `danno-bench-cap-*` temp
root anymore and no `<out>/captures` dir, so there is nothing to strand and nothing to clean.
The report's numeric wire metrics still roll up from the proxy's in-RAM body-free summaries,
so `<out>/metrics` IS written. Exercised by driving the real `danno bench` CLI as a subprocess
so the abort is a genuine signal, not a mock; the stub AI stands in for the model so a full
bench completes in seconds without a GPU. See `.docs/plan-no-capture-truely-does-not-capture.md`.

⚠️ NOT YET LIVE-VERIFIED — see `gates_fixtures` module docstring. The CLI-boundary rows that
DON'T need Docker (the `--no-save-captures --capture-dir` conflict, malformed `[gates]`
rejection) live in the fast suite already (`test_cli`, `test_validator_suites`); this file is
only the rows that need a real run.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from gates_fixtures import MODEL_TAG, PROXY_PORT, STUB_PORT, requires_docker

from book_em_danno.stubai import Finish, StubConfig, ToolLoop, stub_ai

pytestmark = [pytest.mark.slow, requires_docker]

_CAP_PREFIX = "danno-bench-cap-"


def _temp_residue() -> list[Path]:
    """Any legacy temp capture roots — the new design must create NONE."""
    return list(Path(tempfile.gettempdir()).glob(f"{_CAP_PREFIX}*"))


def _write_target(target: Path) -> None:
    """A danno.toml whose one Ollama model dials the stub (bench interposes its always-on
    capture proxy in front) plus a benchmarks.toml with a single tiny task enabled."""
    (target / "danno.toml").write_text(
        "[defaults]\n"
        'default_agent = "build"\n\n'
        "[backends.ollama]\n"
        'kind = "ollama"\n'
        f'base_url = "http://host.docker.internal:{STUB_PORT}/v1"\n\n'
        "[models.stub]\n"
        'backend = "ollama"\n'
        f'tag = "{MODEL_TAG}"\n'
        'reasoning_effort = "none"\n'
        "context_budget = 32000\n"
        "output_limit = 8192\n\n"
        "[agents]\n"
        'build = "stub"\n',
        encoding="utf-8",
    )
    (target / "benchmarks.toml").write_text(
        '[aider_polyglot]\nenabled = true\nselect = ["python/proverb"]\n\n'
        "[gates]\nmax_turns = 5\ntimeout_s = 300\n",
        encoding="utf-8",
    )


@pytest.mark.timeout(1200)
def test_no_save_captures_writes_no_capture_dir_on_completed_run(tmp_path: Path) -> None:
    _write_target(tmp_path)
    out = tmp_path / "out"
    before = set(_temp_residue())
    with stub_ai(
        StubConfig(script=[Finish("done")], transcript_file=tmp_path / "s.jsonl", port=STUB_PORT)
    ):
        proc = subprocess.run(
            [
                "danno",
                "bench",
                "--target",
                str(tmp_path),
                "--no-save-captures",
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=1100,
            check=False,
        )
    assert proc.returncode == 0, proc.stderr
    # No capture data anywhere: no legacy temp root, no <out>/captures, no <out>/transcripts.
    assert not (set(_temp_residue()) - before)
    assert not (out / "captures").exists()
    assert not (out / "transcripts").exists()
    # But the numeric report artifacts ARE written — the run still produced wire metrics.
    assert (out / "bench.json").is_file()
    assert (out / "metrics").is_dir()


@pytest.mark.timeout(1200)
def test_no_save_captures_writes_no_capture_dir_on_sigint_abort(tmp_path: Path) -> None:
    _write_target(tmp_path)
    out = tmp_path / "out"
    stub_transcript = tmp_path / "s.jsonl"
    before = set(_temp_residue())
    # A forever-loop stub so the run is mid-cell when we interrupt it.
    with stub_ai(
        StubConfig(
            script=[ToolLoop("bash", {"command": "true"}, n=None)],
            transcript_file=stub_transcript,
            port=STUB_PORT,
        )
    ):
        proc = subprocess.Popen(
            [
                "danno",
                "bench",
                "--target",
                str(tmp_path),
                "--no-save-captures",
                "--out",
                str(out),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        _wait_for_cell_start(stub_transcript, deadline_s=180)
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)  # Ctrl-C the whole run mid-cell
        proc.wait(timeout=300)
    # Aborted mid-cell, the proxy still wrote nothing: no temp root, no <out>/captures.
    assert not (set(_temp_residue()) - before), "aborted --no-save-captures run stranded captures"
    assert not (out / "captures").exists(), "aborted run created a capture dir under <out>"


def _wait_for_cell_start(stub_transcript: Path, *, deadline_s: float) -> None:
    """Block until the stub has fielded a request (a cell is mid-turn, so the SIGINT lands
    after the proxy is serving) or the deadline passes."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if stub_transcript.is_file() and stub_transcript.stat().st_size > 0:
            return
        time.sleep(1.0)


# Reference: the always-on capture proxy is the gate sensor; PROXY_PORT is opened by the
# bench provisioning. Named here so a reader sees the wiring even though this file drives the
# full CLI (which stands up its own proxy) rather than the per-cell fixture.
_ = PROXY_PORT
