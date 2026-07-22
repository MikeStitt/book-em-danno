# slow-sandbox-tui — results rollup

Per-platform results for the interactive `@slow @sandbox` TUI-launch suite
(`tests/slow/tui/test_tui_launch.py`). Design of record:
[`../plan-slow-sandbox-tui-tests.md`](../plan-slow-sandbox-tui-tests.md).

**No silent green:** a cell is filled only from a real run on that platform×shell. A skip
(runtime down, host-pty backend missing) is recorded as **skip + reason**, never as pass.
Every **break** is root-caused to a file:line and its leg attributed
(**test-harness** / **danno-product** / **runtime**) so it becomes a fix we land in-lane — see
the plan §10.4.

## Rollup matrix

Each per-platform run appends its summary line here (one row per platform×shell).

| platform · shell | driver | fidelity | opencode A/H/C | codex A/H/C | claurst A/H/C | results file |
|---|---|---|---|---|---|---|
| macOS · zsh | PexpectDriver | host-pty | ✅/✅/✅ | ✅/✅/✅ | ✅/✅/`0` | [macos.md](macos.md) |
| WSL2 · bash | PexpectDriver | host-pty | _pending_ | _pending_ | _pending_ | wsl2.md |
| Windows · cmd | WinPtyDriver | host-pty | _pending_ | _pending_ | _pending_ | windows-cmd.md |
| Windows · PowerShell | WinPtyDriver | host-pty | _pending_ | _pending_ | _pending_ | windows-powershell.md |

Legend: ✅ works · ⚠️ degrades · ❌ breaks · ⏭️ skip (reason) · `0` = claurst compacts=False
change-detector asserting `summarization_requests == 0`.

## Per-platform file template

Copy this into `<platform>[-<shell>].md` for each run (dates absolute — scripts can't call
`Date.now`):

```
# <platform> · <shell> — slow-sandbox-tui results
date: 2026-07-DD
host OS / version · shell (cmd|powershell|bash|zsh) · Python x.y.z
sbx <ver> · pexpect <ver> | pywinpty <ver> · driver: PexpectDriver|WinPtyDriver|TmuxDriver(fallback)
fidelity: host-pty (real sbx exec -it)  |  tmux-fallback (record which and why)

| harness  | A | H | C | classification        | leg                                   | notes / root-cause |
|----------|---|---|---|-----------------------|---------------------------------------|--------------------|
| opencode | … | … | … | works/degrades/breaks | test-harness | danno-product | runtime | …                  |
| codex    | … | … | … | works/degrades/breaks | …                                     | …                  |
| claurst  | … | … | 0 | works (compacts=False change-detector) | —                    | …                  |
```
