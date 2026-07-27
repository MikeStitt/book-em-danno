# Windows-lane progress — slow-sandbox-tui suite

**Date:** 2026-07-22 (updated)
**Work order:** `.docs/2026-07-22-windows-handoff-slow-tui.md`
**Branch:** `slow-sandbox-tui-tests-windows` (off `slow-sandbox-tui-tests` tip `ba53927`)

## Status: Windows-native + WSL2 lanes DONE — opencode + codex + claurst all green on all three (cmd, PowerShell, WSL2)

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

### claurst x86_64 artifact — RESOLVED (2026-07-27)
- The former blocker (release shipped only `claurst-linux-aarch64.tar.gz`) is **closed**. Built
  `claurst-linux-x86_64.tar.gz` natively (amd64, clean `rust:1-bookworm`, from `danno-integration`
  tip `dafbde1` ≡ release `d466ec4`) and published it to the **existing** `v0.1.6-danno1` release
  on `MikeStitt/claurst` via `gh release upload` — aarch64 asset + npm + release notes untouched.
  `sha256:fcf9e047a37b3316074ee7a070ceda22b23ecd876dd14c74bd63667b4a769d08`, 14583484 bytes.
  (Windows `gh` had its own per-machine token with `repo` write, per the handoff.)
- **Re-ran the previously-red leg on all three lanes → green:** claurst A/H/`0`
  (`1 passed`) on Windows-cmd (215s), Windows-PowerShell (249s), WSL2 (301s). The arch-aware
  installer selects `x86_64` and fetches with no 404. Results files + README rollup updated.
- Follow-up (not this branch): the arch-aware `claurst.py` selector still needs to merge forward
  onto the base branches (`harness-api-add-codex`), which still hardcode `aarch64`.

### P3 — WSL2 (DONE)
- Reused `PexpectDriver` unchanged; ext4 checkout (`~/book-em-danno`); `sbx` standalone in WSL2
  (no Docker Desktop integration needed). One env blocker fixed: added user to `kvm` group
  (`/dev/kvm` perms) + `wsl --terminate`. P1.5 smoke PASS; fast gate 683 passed; opencode+codex
  A/H/C green; claurst blocked on the same x86_64 artifact (WSL2 is x86_64). See `wsl2.md`.

## Key rules
- Do NOT modify the frozen [mac]-owned shared test files except the `WinPtyDriver` body (done).
- Fix-in-lane danno-product breaks; never weaken sandbox egress or force tests green.
- Push/merge only when the user asks. Commits so far are local on the branch.
