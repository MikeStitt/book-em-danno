"""#108: provision the bench sandbox through bench's ACTUAL caller, not the gate-fixture
shortcut, for a name != image harness.

The fast gate is Docker-free and the other slow gates reach a sandbox via
`gates_fixtures.provisioned_sandbox` — which calls `sandbox.provision` DIRECTLY, bypassing
`_run_aider`/`_run_swebench`. So a caller-contract regression (b1fcfea: passing
`Harness.sandbox_image` instead of the harness NAME, which made provision look up an
unregistered harness `shell` and abort the whole sweep) sailed through both gates into a
live multi-hour run. The fast unit guards assert the value handed to `sb.provision`; THIS
test drives the real create+install path through `_run_swebench` for claurst (which rides
the `shell` image, so name != image — the exact case the bug crashed on).

`variants=[]` + a no-op stubbed task means only the provision + install steps run (the
crash site), not any model turn — so the test is a focused create+install smoke, not a full
bench cell.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from gates_fixtures import PROXY_PORT, gen_config, requires_docker, teardown_sandbox

from book_em_danno.config.generate import generate
from book_em_danno.core.exec import Runner
from danno_validator.suites import bench

pytestmark = [pytest.mark.slow, requires_docker]


class _FakeSweTask:
    """A swebench task whose per-task work is a no-op: `_run_swebench` provisions + installs
    the harness BEFORE calling `task.provision`, and with no variants there is no cell to run,
    so reaching this stub means the real create+install path completed without raising."""

    id = "stub-108"

    def provision(self, runner: object, name: str, workspace: object) -> None:
        pass


@pytest.mark.timeout(1200)
@pytest.mark.parametrize("harness", ["claurst"])  # name != image: claurst rides the `shell` image
def test_run_swebench_provisions_and_installs_through_real_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    monkeypatch.setattr(bench, "load_swebench_tasks", lambda *a, **k: [_FakeSweTask()])
    cfg = bench.BenchmarksConfig()
    cfg.swebench.enabled = True
    cfg.swebench.select = ["stub-108"]

    config = gen_config(harness)
    generate(config, tmp_path, apply=True)
    name = bench._sandbox_name(tmp_path, "swe-stub-108")
    teardown_sandbox(name)  # clear any orphan from a prior run so provision really creates
    opts = bench.BenchOptions(target=tmp_path, harness=harness, out_dir=tmp_path / "out")
    try:
        # Real sbx provision + harness install through the ACTUAL bench caller. Under the old
        # bug this raised `unknown harness 'shell'`; the fix passes the name so it resolves the
        # `shell` image and installs claurst into the VM. No variants → no model turn.
        verdicts = bench._run_swebench(
            Runner(apply=True),
            cfg,
            opts,
            workspace=tmp_path,
            variants=[],
            config=config,
            env_files={},
            capture=None,
            sampler=None,
            allow_hosts=(f"localhost:{PROXY_PORT}",),
            capture_port=None,
            warm=False,
            warmup=[],
            harness_ident={},
            egress_logs={},
        )
        # zero variants → zero rows; the point is that create+install did not raise.
        assert verdicts == []
    finally:
        teardown_sandbox(name)  # _run_swebench tears down too; belt-and-suspenders on any raise
