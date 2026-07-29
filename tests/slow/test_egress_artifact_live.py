"""#101: prove the egress reachability artifact against the REAL `sbx policy log --json`.

The fast unit suite pins `DANNO_SANDBOX_CLI=docker`, so `policy_log_argv` returns `None` and
the whole egress path no-ops without shelling out — which means the fast gate never touches
sbx's actual `policy log` CLI or its JSON shape. This slow test closes that gap: it provisions a
real sbx sandbox and drives `record_egress` end-to-end, asserting a well-formed
`<capture_dir>/egress.json` lands with the run stamp, note, and the sandbox's snapshot.

A network-quiet sandbox is fine — `sbx policy log` returns the daemon's `{allowed_hosts,
blocked_hosts}` object (possibly empty) regardless, and that object IS the artifact payload.
Skips loud when the sandbox runtime is down; never a silent pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gates_fixtures import provisioned_sandbox
from sandbox_runtime import sandbox_runtime_down

from book_em_danno.capture.egress import EGRESS_NOTE, record_egress
from book_em_danno.commands import sandbox_cli
from book_em_danno.core.exec import Runner

pytestmark = [pytest.mark.slow, pytest.mark.sandbox]


@pytest.mark.skipif(sandbox_runtime_down(), reason="sandbox runtime down (sbx ls)")
@pytest.mark.timeout(1200)
def test_record_egress_writes_artifact_against_real_sbx_policy_log(tmp_path: Path) -> None:
    if sandbox_cli.resolve_backend() != "sbx":
        pytest.skip("egress artifact is sbx-only (docker has no `policy log` equivalent)")

    name = "danno-egress-101"
    run_start = "2026-07-29T00:00:00+00:00"  # a fixed stamp (Date.now unavailable in scripts)
    capture_dir = tmp_path / "captures"
    with provisioned_sandbox(name, "opencode", tmp_path):
        path = record_egress(
            Runner(apply=True), [name], capture_dir=capture_dir, run_start=run_start
        )

    assert path is not None, "real `sbx policy log --json` must yield a JSON object to record"
    assert path == capture_dir / "egress.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_start"] == run_start
    assert payload["note"] == EGRESS_NOTE
    # The snapshot is keyed by sandbox name; its value is sbx's own reachability object.
    assert name in payload["sandboxes"]
    snap = payload["sandboxes"][name]
    assert isinstance(snap, dict)
    # sbx's network log carries these two buckets (either may be empty on a quiet sandbox).
    assert "allowed_hosts" in snap or "blocked_hosts" in snap
