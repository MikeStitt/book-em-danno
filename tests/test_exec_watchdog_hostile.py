"""V2 — the runaway-gate watchdog under hostile children (the F2/F3 net).

Real subprocesses, no Docker. `Runner.capture` under `watching()` takes the watched path
(`core.exec._capture_watched`). The two hardened behaviors:

- **F2** — the kill covers the process GROUP (`start_new_session` + `killpg`) and
  `reader.join()` is bounded, so a grandchild inheriting the stdout pipe is reaped too and
  `capture` returns promptly after the gate fires (it never hangs on the still-open pipe).
  The row uses a **short-lived** grandchild so that even a regression latency-fails rather
  than hanging the gate (the same "fail, never hang" rule the whole plan rests on).
- **F3** — reader-thread decoding is best-effort (`errors="replace"`) and unexpected reader
  errors are surfaced, so a child emitting invalid UTF-8 no longer returns a silent empty
  string.

The other two rows (kill-latency, watched/unwatched parity) are standing regression nets.
See `.docs/plan-runaway-gates-validation.md` §2.2/§2.3/§4.
"""

from __future__ import annotations

import sys
import time

import pytest

from book_em_danno.core.exec import Runner

pytestmark = pytest.mark.timeout(15)

# A parent that spawns a grandchild inheriting its stdout pipe, then loops forever so the
# watchdog must kill it. The grandchild outlives the parent's kill but is bounded (2 s):
# under F2 the reader stays blocked on the still-open pipe until the grandchild exits, so
# `capture` returns ~2 s late instead of promptly. The fix (process-group kill) reaps the
# grandchild too, so `capture` returns at once.
_GRANDCHILD_HOLD_S = 2.0
_PARENT_HOLDS_PIPE = (
    "import subprocess, sys, time; "
    f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({_GRANDCHILD_HOLD_S})']); "
    "sys.stdout.flush()\n"
    "while True: time.sleep(0.05)"
)


def test_grandchild_holding_pipe_does_not_delay_return() -> None:
    runner = Runner()
    cmd = [sys.executable, "-c", _PARENT_HOLDS_PIPE]
    start = time.monotonic()
    with runner.watching(timeout_s=0.3) as watch:
        runner.capture(cmd)
    elapsed = time.monotonic() - start
    assert watch.breach is not None and watch.breach.gate == "timeout"
    # Prompt return after the kill: the fix reaps the grandchild, so this is ~0.5 s. Today
    # capture() waits out the grandchild (~2 s), overshooting the bound → red.
    assert elapsed < 1.5


def test_invalid_utf8_output_is_not_silently_dropped() -> None:
    runner = Runner()
    # A child that emits bytes undecodable as UTF-8, then exits cleanly (no gate breach).
    cmd = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe done')"]
    with runner.watching():  # no limits, but the watched path is still taken
        result = runner.capture(cmd)
    # The unwatched path (`subprocess.run(text=True)`) would raise loudly; the watched path
    # must not diverge into a silent "". The fix decodes with errors="replace".
    assert result.stdout != ""


def test_gate_kill_latency_is_bounded() -> None:
    runner = Runner()
    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    start = time.monotonic()
    with runner.watching(timeout_s=0.5) as watch:
        runner.capture(cmd)
    elapsed = time.monotonic() - start
    assert watch.breach is not None and watch.breach.gate == "timeout"
    # A breach is enacted within 2 × _WATCH_INTERVAL_S past the deadline, plus kill/reap
    # overhead — comfortably under 3 s for a well-behaved sleeper.
    assert elapsed < 3.0


def test_watched_matches_unwatched_for_well_behaved_child() -> None:
    runner = Runner()
    cmd = [
        sys.executable,
        "-c",
        "import sys; print('to stdout'); print('to stderr', file=sys.stderr); sys.exit(3)",
    ]
    unwatched = runner.capture(cmd)
    with runner.watching(timeout_s=30):  # ample; the child exits on its own
        watched = runner.capture(cmd)
    assert (watched.stdout, watched.stderr, watched.returncode) == (
        unwatched.stdout,
        unwatched.stderr,
        unwatched.returncode,
    )
    assert unwatched.returncode == 3
