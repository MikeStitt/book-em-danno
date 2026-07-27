"""The parametrized A/H/C interactive-launch test over [opencode, codex, claurst].

For each in-scope harness this drives danno's REAL `sbx exec -it … <argv>` frame through a host
pty and asserts against the captured HTTP wire ("wire, not paint",
`.docs/plan-slow-sandbox-tui-tests.md` §2/§8.4):

- **A — reach TUI:** launch, wait through first-run dialogs, assert banner + composer on screen.
- **H — turn on wire:** submit a '?'-free prompt, confirm ≥1 inference request landed.
- **C — compaction:** with the first reply's usage inflated, drive more turns, then assert on
  `summarization_requests` BRANCHED on the harness's `compacts` capability (§5) — a positive
  assert for codex/opencode, a `== 0` change-detector for claurst.

Marked `slow` (out of the fast gate) + `sandbox` (`-m sandbox` selects just these). Skips loud
when the sandbox runtime is down or the platform's host-pty backend is unavailable — never a
silent pass.
"""

from __future__ import annotations

import contextlib

import pytest
from gates_fixtures import provisioned_sandbox, scripted_backend
from sandbox_runtime import sandbox_runtime_down

import book_em_danno.stubai.script as stub_script
from book_em_danno.stubai import Finish
from danno_validator.harnesses import get

from .driver import DriverUnavailable, make_driver
from .fixtures import WireMetrics, codex_compact_graft, launch_argv
from .primitives import HARNESS, one_shot_inflate, settle_and_dismiss, submit

pytestmark = [pytest.mark.slow, pytest.mark.sandbox]


@pytest.mark.skipif(sandbox_runtime_down(), reason="sandbox runtime down (sbx ls)")
@pytest.mark.parametrize("harness", ["opencode", "codex", "claurst"])
def test_interactive_launch(harness: str, tmp_path) -> None:  # type: ignore[no-untyped-def]
    cfg = HARNESS[harness]
    name = f"danno-tui-{harness}"

    # Inflate the FIRST stub reply's usage only (else unbounded compaction runaway, §6). Restore
    # in finally so the global monkeypatch never leaks to another test.
    restore = one_shot_inflate(stub_script.ScriptEngine, cfg.inflate)
    try:
        with contextlib.ExitStack() as stack:
            # codex needs a small top-level auto-compact limit to compact under inflated usage;
            # entered BEFORE provisioning so the graft is live when its config.toml is written.
            if cfg.graft_compact_limit:
                stack.enter_context(codex_compact_graft())
            backend = stack.enter_context(
                scripted_backend([Finish("Hello from the stub.")], tmp_path)
            )
            target = stack.enter_context(provisioned_sandbox(name, harness, tmp_path))

            exe, args, env = launch_argv(harness, cfg, name=name, target=target)
            try:
                driver = make_driver([exe, *args], env, prefer="auto")
            except DriverUnavailable as e:
                pytest.skip(str(e))  # loud, named skip — never a silent pass
            driver.start()
            try:
                wire = WireMetrics(backend.capture_file, cfg)

                # A — reach the TUI.
                assert driver.pump(90, want=cfg.banner), (
                    f"{harness}: TUI banner {cfg.banner} never appeared"
                )
                settle_and_dismiss(driver, cfg)
                screen = driver.screen().lower()
                assert any(c in screen for c in cfg.composer), (
                    f"{harness}: composer marker {cfg.composer} not on screen"
                )
                assert "not a terminal" not in screen and "is not a tty" not in screen, (
                    f"{harness}: pty nesting failed (not-a-terminal error on screen)"
                )

                # H — one turn on the wire.
                assert submit(
                    driver, cfg, wire, "list the files here", want="hello from the stub"
                ), f"{harness}: no turn landed on the wire"
                assert wire.requests() >= 1

                # C — compaction, branched on the harness's declared capability (§5).
                for prompt in ("and again please", "one more time"):
                    submit(driver, cfg, wire, prompt)
                driver.pump(5)
                summ = wire.summarization_requests()
                if get(harness).compacts:
                    assert summ >= 1, (
                        f"{harness} declares compacts=True but no summarization request "
                        f"reached the wire (items={wire.item_counts()})"
                    )
                else:
                    # claurst change-detector: if it EVER starts compacting, this goes RED loud,
                    # forcing a conscious flip to compacts=True (fail-loud, never silent).
                    assert summ == 0, (
                        f"{harness} declares compacts=False but {summ} summarization request(s) "
                        f"reached the wire — flip compacts=True + changelog + version bump"
                    )
            finally:
                driver.close()
    finally:
        restore()
