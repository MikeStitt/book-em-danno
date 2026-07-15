from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.commands import sandbox_cli
from book_em_danno.core.exec import Runner


@pytest.fixture(autouse=True)
def _pin_sandbox_cli(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the sandbox CLI to the legacy `docker sandbox` so argv assertions are
    deterministic regardless of whether `sbx` is installed on the host. Tests that
    exercise the sbx backend override with `monkeypatch.setenv("DANNO_SANDBOX_CLI",
    "sbx")` (or `delenv(..., raising=False)` to test auto-detection).

    EXCEPTION — the slow tier (`@pytest.mark.slow`): those tests drive a REAL sandbox, so
    they must use the CLI that is actually installed (auto-detected: `sbx` on this host),
    not this pin. Their module-scoped `provisioned_sandbox` CREATES the sandbox with the
    auto-detected CLI (module-scoped setup runs before this function-scoped autouse), then
    each test body EXECs a turn; if that exec ran under the pinned `docker sandbox` while
    the sandbox was created by `sbx`, it would target a sandbox the legacy CLI cannot see →
    sandbox-not-found → the harness never starts → 0 inference calls, silently (the driver
    swallows non-zero HUT exits). That mismatch is exactly what once made the whole opencode
    matrix fail with `rounds == 0` while a standalone repro of the same fixtures passed. So
    for slow tests we `delenv` instead of pinning, keeping provision and exec on one CLI.
    (This lives here rather than in a `tests/slow/conftest.py` because a second top-level
    `conftest` module would shadow this one and break `from conftest import RecordingRunner`
    in the root tests.)"""
    sandbox_cli.set_backend(None)  # reset the module-level override between tests
    if request.node.get_closest_marker("slow") is not None:
        monkeypatch.delenv("DANNO_SANDBOX_CLI", raising=False)
        return
    monkeypatch.setenv("DANNO_SANDBOX_CLI", "docker")


class RecordingRunner(Runner):
    """A Runner that records advised/run commands instead of executing, for
    asserting exact command construction and ordering without a Docker/Ollama
    daemon. Records both `advise` (gated) and `run` (always-execute) so launch and
    shell are captured rather than shelling out."""

    def __init__(self) -> None:
        super().__init__(apply=False, verbose=False)
        self.commands: list[list[str]] = []

    def advise(
        self,
        cmd: list[str],
        why: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        self.commands.append(cmd)
        return cmd

    def run(
        self,
        cmd: list[str],
        why: str,
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> list[str]:
        self.commands.append(cmd)
        return cmd

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.commands]
