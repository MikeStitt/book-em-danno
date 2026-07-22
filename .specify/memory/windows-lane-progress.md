# Windows-lane progress — slow-sandbox-tui suite

**Date:** 2026-07-22 (updated)
**Work order:** `.docs/2026-07-22-windows-handoff-slow-tui.md`
**Branch:** `slow-sandbox-tui-tests-windows` (off `slow-sandbox-tui-tests` tip `ba53927`)

## Status: Windows-native + WSL2 lanes DONE (opencode+codex green on all three); claurst blocked on external x86_64 artifact

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

### P3 — WSL2 (DONE)
- Reused `PexpectDriver` unchanged; ext4 checkout (`~/book-em-danno`); `sbx` standalone in WSL2
  (no Docker Desktop integration needed). One env blocker fixed: added user to `kvm` group
  (`/dev/kvm` perms) + `wsl --terminate`. P1.5 smoke PASS; fast gate 683 passed; opencode+codex
  A/H/C green; claurst blocked on the same x86_64 artifact (WSL2 is x86_64). See `wsl2.md`.

## Key rules
- Do NOT modify the frozen [mac]-owned shared test files except the `WinPtyDriver` body (done).
- Fix-in-lane danno-product breaks; never weaken sandbox egress or force tests green.
- Push/merge only when the user asks. Commits so far are local on the branch.
