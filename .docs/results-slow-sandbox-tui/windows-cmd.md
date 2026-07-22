# Windows · cmd — slow-sandbox-tui results

date: 2026-07-22
host OS / version: Windows 11 Pro 10.0.26200 · shell: cmd · Python 3.14.2 (uv venv)
sbx v0.34.0 · pywinpty 3.0.5 · pyte 0.8.2 · driver: WinPtyDriver
fidelity: host-pty (real `sbx exec -it`, ConPTY via pywinpty)

Run from a real `cmd.exe` outer shell (batch file), so the cmd env-var/quoting surface
(H1–H10) is exercised in addition to the driver. `WinPtyDriver` is a single codebase; only the
outer shell differs from the PowerShell run.

| harness  | A | H | C | classification | leg           | notes / root-cause |
|----------|---|---|---|----------------|---------------|--------------------|
| opencode | ✅ | ✅ | ✅ | works          | —             | A/H/C green from cmd.exe (`2 passed in 291s` with codex). |
| codex    | ✅ | ✅ | ✅ | works          | —             | Green from cmd.exe; compaction request on the wire. |
| claurst  | ⏭️ | — | — | not run from cmd | danno-product → **blocked on release artifact** | Not re-run from cmd: the failure is the claurst **install** (missing `claurst-linux-x86_64.tar.gz` in the `MikeStitt/claurst` `v0.1.6-danno1` release), which happens inside the VM **before** the pty/driver and is **shell-independent** — identical to the PowerShell run. See [windows-powershell.md](windows-powershell.md). danno is now arch-aware (fixed in-lane); re-run once the x86_64 asset is published. |

## Notes

- Same runtime/egress/P1.5 findings as [windows-powershell.md](windows-powershell.md) — the
  driver and danno-product paths are identical; cmd vs PowerShell differ only in the outer
  shell's env-var syntax and quoting, which danno sidesteps by building argv **lists** (never
  shell strings) and forwarding env by `-e NAME`.
