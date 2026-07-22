# Windows-lane progress — slow-sandbox-tui suite

**Date:** 2026-07-22 (updated)
**Work order:** `.docs/2026-07-22-windows-handoff-slow-tui.md`
**Branch:** `slow-sandbox-tui-tests-windows` (off `slow-sandbox-tui-tests` tip `ba53927`)

## Status: Windows-native lane DONE (opencode+codex green); claurst blocked on external artifact; WSL2 (P3) not started

### Completed
- **§2 Preconditions** — all confirmed: `sbx` resolves (after `sbx login`); Python 3.13+/uv;
  pywinpty 3.0.5 + pyte 0.8.2 import; ConPTY API verified by exercising it.
- **P1.5 smoke** — PASS against live sbx: `-it` exec, `-e NAME` forward (issue #99), `-w`
  container path, readable direct-mount. Found + fixed the danno-product `-w`/container-path bug.
- **P2 WinPtyDriver** — implemented (pywinpty/ConPTY, non-blocking read via `fileobj.settimeout`,
  `pyte.Stream`). **opencode + codex A/H/C green on BOTH cmd and PowerShell.** pywinpty pinned
  `==3.0.5`.
- **Fast gate green on Windows** (683 passed) via fix-in-lane danno-product fixes (5 commits):
  watchdog `taskkill /T`; UTF-8 writes; `container_path()`; claurst arch-aware; + WinPtyDriver.
- **Results written:** `windows-cmd.md`, `windows-powershell.md`, rollup README updated;
  `plan-test-danno-cross-platform.md` fix record appended.

### Blocked (external artifact — NOT danno code, NOT runtime absence)
- **claurst on Windows-x86_64:** danno is now arch-aware and fails **loud (404)**, but the
  `MikeStitt/claurst` `v0.1.6-danno1` release has only `claurst-linux-aarch64.tar.gz`. Needs a
  `claurst-linux-x86_64.tar.gz` build published. No `gh`/token locally to publish; user decision
  pending. Build is feasible inside the x86_64 sandbox VM (rustup + cargo).

### Not started
- **P3 — WSL2 validation** (reuse `PexpectDriver`; needs Docker Desktop WSL integration +
  ext4 checkout + P1.5 smoke inside WSL2).

## Key rules
- Do NOT modify the frozen [mac]-owned shared test files except the `WinPtyDriver` body (done).
- Fix-in-lane danno-product breaks; never weaken sandbox egress or force tests green.
- Push/merge only when the user asks. Commits so far are local on the branch.
