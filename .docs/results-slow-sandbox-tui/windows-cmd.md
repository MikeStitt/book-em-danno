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
| claurst  | ✅ | ✅ | `0` | works          | —             | A/H/`0` green from a real `cmd.exe` outer shell (`1 passed in 215s`, 2026-07-27), once the x86_64 release artifact was published (see [windows-powershell.md](windows-powershell.md) → "claurst x86_64 release artifact"). The arch-aware installer (`uname -m` → `x86_64`) fetches `claurst-linux-x86_64.tar.gz` with no 404. `0` = compacts=False change-detector asserting `summarization_requests == 0`. |

## Notes

- Same runtime/egress/P1.5 findings as [windows-powershell.md](windows-powershell.md) — the
  driver and danno-product paths are identical; cmd vs PowerShell differ only in the outer
  shell's env-var syntax and quoting, which danno sidesteps by building argv **lists** (never
  shell strings) and forwarding env by `-e NAME`.
