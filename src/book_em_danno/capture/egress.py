"""Snapshot the sandbox egress reachability log (`sbx policy log`) as a `--capture`
artifact (issue #101).

The L7 wire capture (`captures/**.jsonl`, from `capture.proxy`) records the *content*
of harness↔backend traffic — request/response bodies, per call, secrets redacted. This
module records the complementary **host-side L3/L4 audit**: every host a sandbox tried to
reach, allowed or blocked, aggregated per host with `proxy_type`/`rule`/`since`/`last_seen`/
`count_since`. It answers "did the sandbox try to phone anywhere it shouldn't have?" — the
capture proxy cannot, because a blocked connection never produces a request body.

It is emitted for **every** `--capture` run that persists artifacts — `sandbox start`,
`sandbox shell`, `validate`, and `bench` — as a uniform `<capture_dir>/egress.json`.

Documented limits (also carried in the artifact's own `note`):
- **Aggregated, not per-request**: one row per host (`since`→`last_seen`, `count_since`),
  no per-request timestamps and no HTTP status — that granularity is the L7 wire capture.
- **Per-sandbox, not per-cell**: bench's per-`(harness,model,task)` split lives in the
  wire capture; this log is keyed by sandbox name.
- **Cross-run bleed on reuse**: `sbx policy log` reads the daemon's history, which persists
  across `sbx rm` and sandbox reuse. A stable-named sandbox (interactive start/shell) can
  therefore carry rows from earlier runs; the artifact records `run_start` so a reader can
  isolate this run with `last_seen >= run_start`.
- **sbx-only**: legacy `docker sandbox` exposes no equivalent, so the artifact is silently
  omitted on that backend (`sandbox_cli.policy_log_argv` → `None`).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from book_em_danno.commands import sandbox_cli
from book_em_danno.core.exec import Runner, log_info, log_warn

EGRESS_NOTE = (
    "sbx policy log (network): every host each sandbox tried to reach, allowed or blocked, "
    "aggregated per host (since->last_seen, count_since). L3/L4 host-side audit — no "
    "per-request timestamps and no HTTP status; that is the L7 wire capture under captures/. "
    "Keyed per sandbox, not per bench cell. The daemon log persists across sandbox reuse, so "
    "for a reused (stable-named) sandbox filter last_seen >= run_start to isolate this run."
)


def snapshot_egress_log(runner: Runner, sandbox: str) -> dict | None:
    """Best-effort `sbx policy log <sandbox> --type network --json`, parsed to its
    `{allowed_hosts, blocked_hosts}` dict.

    Host-side, so it does not exec into (or perturb) the sandbox and works whether or not
    the VM is still running. Returns `None` — never raises — on the docker backend (no
    equivalent), when the probe cannot run or exits non-zero, or when the output is not a
    JSON object. A missing egress log is a null artifact, never a run failure (Working
    Rule 8 is served by `warn_on_blocked`, not by aborting the run).
    """
    argv = sandbox_cli.policy_log_argv(sandbox)
    if argv is None:
        return None
    try:
        result = runner.capture(argv)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def warn_on_blocked(sandbox: str, snapshot: dict) -> None:
    """Fail loud (Working Rule 8): a captured run that silently hit an egress block is a
    sandbox-leg defect, not a clean result — name the blocked hosts on stderr."""
    raw = snapshot.get("blocked_hosts")
    blocked = [b for b in raw if isinstance(b, dict)] if isinstance(raw, list) else []
    if not blocked:
        return
    hosts = ", ".join(sorted({str(b.get("host", "?")) for b in blocked}))
    log_warn(
        f"egress: sandbox {sandbox!r} hit {len(blocked)} BLOCKED host(s): {hosts}. A captured "
        "run that silently hit egress blocks is a sandbox-leg defect — see egress.json."
    )


def accumulate_egress(runner: Runner, sandbox: str, into: dict[str, dict]) -> None:
    """Snapshot `sandbox` (fail-loud on blocks) and, if captured, record it under its name in
    `into`. For callers that snapshot sandboxes one at a time before tearing each down (bench),
    accumulating into a shared dict written once with `write_egress_artifact`."""
    snapshot = snapshot_egress_log(runner, sandbox)
    if snapshot is None:
        return
    into[sandbox] = snapshot
    warn_on_blocked(sandbox, snapshot)


def write_egress_artifact(
    snapshots: dict[str, dict], *, capture_dir: Path, run_start: str
) -> Path | None:
    """Write the accumulated per-sandbox snapshots to `<capture_dir>/egress.json`, or return
    `None` without writing when nothing was captured (docker backend, all probes failed, or no
    sandboxes) so no empty file is left behind."""
    if not snapshots:
        return None
    payload = {"run_start": run_start, "note": EGRESS_NOTE, "sandboxes": snapshots}
    capture_dir.mkdir(parents=True, exist_ok=True)
    path = capture_dir / "egress.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    log_info(f"egress: wrote reachability log for {len(snapshots)} sandbox(es) -> {path}")
    return path


def record_egress(
    runner: Runner, sandboxes: Sequence[str], *, capture_dir: Path, run_start: str
) -> Path | None:
    """Snapshot every sandbox in `sandboxes` (de-duplicated, order-preserving) and write the
    combined `<capture_dir>/egress.json`. The one-shot convenience for callers that know all
    their sandbox(es) at teardown (validate, sandbox start/shell). Returns the artifact path,
    or `None` when nothing was captured."""
    acc: dict[str, dict] = {}
    for name in dict.fromkeys(sandboxes):
        accumulate_egress(runner, name, acc)
    return write_egress_artifact(acc, capture_dir=capture_dir, run_start=run_start)
