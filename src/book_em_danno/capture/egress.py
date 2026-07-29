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
    the VM is still running. A missing egress log is a null artifact, never a run failure —
    Working Rule 8 is served by `warn_on_blocked`, not by aborting the run — so every path
    returns `None` rather than raising. But **absent and failed are different facts** (policy
    §5): the docker backend has no such log and returns `None` **silently**, while a probe
    that could not run, exited non-zero, or produced non-object / unparseable output is a
    genuine anomaly during a `--capture` run — the security audit the operator asked for
    silently went missing — and each **WARNs** (greppably) before returning `None`.
    """
    argv = sandbox_cli.policy_log_argv(sandbox)
    if argv is None:
        return None  # docker backend: no egress log exists — legitimately absent, not a failure
    try:
        result = runner.capture(argv)
    except OSError as exc:
        log_warn(
            f"egress: could not run the reachability probe for {sandbox!r}; omitting it from "
            f"egress.json ({exc})"
        )
        return None
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "(no output)"
        log_warn(
            f"egress: `sbx policy log` exited {result.returncode} for {sandbox!r}; omitting it "
            f"from egress.json ({detail})"
        )
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        log_warn(
            f"egress: reachability log for {sandbox!r} was unparseable JSON; omitting it from "
            f"egress.json ({exc})"
        )
        return None
    if not isinstance(data, dict):
        log_warn(
            f"egress: reachability log for {sandbox!r} was not a JSON object; omitting it from "
            "egress.json"
        )
        return None
    return data


def warn_on_blocked(sandbox: str, snapshot: dict) -> None:
    """Fail loud (Working Rule 8): a captured run that silently hit an egress block is a
    sandbox-leg defect, not a clean result — name the blocked hosts on stderr."""
    raw = snapshot.get("blocked_hosts")
    if raw is None:
        return  # no blocked_hosts key: legitimately no blocks recorded — silent (absent ≠ failed)
    if not isinstance(raw, list):
        # A non-list here would otherwise read as "hit no blocks" and hide a real egress block
        # (the exact failure this audit exists to catch) — surface the malformed shape (policy §5).
        log_warn(
            f"egress: sandbox {sandbox!r} reported a non-list blocked_hosts "
            f"({type(raw).__name__}); cannot confirm it hit no egress blocks — see egress.json."
        )
        return
    blocked = [b for b in raw if isinstance(b, dict)]
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
    path = capture_dir / "egress.json"
    try:
        capture_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        # The egress log is a companion audit, never a command's data product (that is already
        # durably written by the time we get here). A failed write must not crash an otherwise-
        # good run — nor, in bench's teardown `finally`, mask the real exception — so WARN and
        # drop it rather than raise (policy §5, WARNING). This keeps `record_egress` safe to call
        # from a `finally`: none of its primitives raise.
        log_warn(f"egress: could not write the reachability log to {path} ({exc})")
        return None
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
