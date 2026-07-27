"""Shared liveness probe for the slow suite's sandbox-driving tests.

These tests drive whichever sandbox runtime danno itself would pick — `sbx` when
it is installed, else legacy `docker sandbox` — so the skip guard must probe THAT
runtime. The old guard probed the standalone `docker` daemon (`docker info`),
which on an sbx host can be down even while sbx's own runtime is up (`sbx ls`
works): a false-negative that skipped every live test even when the sandbox was
usable. This resolves the backend exactly as danno does and probes it directly.
"""

from __future__ import annotations

import shutil
import subprocess

from book_em_danno.commands import sandbox_cli


def sandbox_runtime_down() -> bool:
    """True when the resolved sandbox runtime can't be reached (so a live sandbox
    test should skip). Probes the same backend danno would use: `sbx ls` for sbx
    (needs its runtime up), `docker info` for legacy docker."""
    backend = sandbox_cli.resolve_backend()
    probe = ["sbx", "ls"] if backend == "sbx" else ["docker", "info"]
    if shutil.which(probe[0]) is None:
        return True
    return subprocess.run(probe, capture_output=True, check=False).returncode != 0
