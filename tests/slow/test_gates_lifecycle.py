"""V4 — config-is-code capture lifecycle (GV3, Tier B).

The F4 fix in-sandbox: `--no-save-captures` must leave NO `danno-bench-cap-*` residue after
either a completed run OR an aborted one (SIGINT mid-run) — exercised by driving the real
`danno bench` CLI as a subprocess so the abort is a genuine signal, not a mock. The stub AI
stands in for the model so a full bench completes in seconds without a GPU.

⚠️ NOT YET LIVE-VERIFIED — see `gates_fixtures` module docstring. The CLI-boundary rows that
DON'T need Docker (the `--no-save-captures --capture-dir` conflict, malformed `[gates]`
rejection) live in the fast suite already (`test_cli`, `test_validator_suites` / the F4
commit); this file is only the rows that need a real run.
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


def _residue() -> list[Path]:
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
        '[aider_polyglot]\nenabled = true\nselect = ["python/anagram"]\n\n'
        "[gates]\nmax_turns = 5\ntimeout_s = 300\n",
        encoding="utf-8",
    )


@pytest.mark.timeout(1200)
def test_no_save_captures_leaves_no_residue_on_completed_run(tmp_path: Path) -> None:
    _write_target(tmp_path)
    before = set(_residue())
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
                str(tmp_path / "out"),
            ],
            capture_output=True,
            text=True,
            timeout=1100,
            check=False,
        )
    assert proc.returncode == 0, proc.stderr
    assert not (set(_residue()) - before)  # completed run persisted nothing


@pytest.mark.timeout(1200)
def test_no_save_captures_leaves_no_residue_on_sigint_abort(tmp_path: Path) -> None:
    _write_target(tmp_path)
    before = set(_residue())
    # A forever-loop stub so the run is mid-cell when we interrupt it.
    with stub_ai(
        StubConfig(
            script=[ToolLoop("bash", {"command": "true"}, n=None)],
            transcript_file=tmp_path / "s.jsonl",
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
                str(tmp_path / "out"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        _wait_for_temp_root(before, deadline_s=180)
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)  # Ctrl-C the whole run mid-cell
        proc.wait(timeout=300)
    # The F4 finally must have removed the temp capture root despite the abort.
    assert not (set(_residue()) - before), "aborted --no-save-captures run stranded captures"


def _wait_for_temp_root(before: set[Path], *, deadline_s: float) -> None:
    """Block until bench has created its temp capture root (so the SIGINT lands after setup,
    mid-run) or the deadline passes."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if set(_residue()) - before:
            return
        time.sleep(1.0)


# Reference: the always-on capture proxy is the gate sensor; PROXY_PORT is opened by the
# bench provisioning. Named here so a reader sees the wiring even though this file drives the
# full CLI (which stands up its own proxy) rather than the per-cell fixture.
_ = PROXY_PORT
