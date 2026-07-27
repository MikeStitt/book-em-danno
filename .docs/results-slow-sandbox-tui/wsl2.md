# WSL2 · bash — slow-sandbox-tui results

date: 2026-07-22
host OS / version: WSL2 Ubuntu (on Windows 11 26200), Linux x86_64 · shell: bash · Python 3.14.3 (uv venv)
sbx v0.34.0 · pexpect 4.9.0 · pyte 0.8.2 · driver: PexpectDriver
fidelity: host-pty (real `sbx exec -it`, Unix pty via pexpect)

WSL2 is Linux, so the **reused `PexpectDriver` runs unchanged** — no new driver code. Checkout on
native **ext4** (`~/book-em-danno`, cloned from the Windows working tree), not `/mnt/c`.

| harness  | A | H | C | classification | leg           | notes / root-cause |
|----------|---|---|---|----------------|---------------|--------------------|
| opencode | ✅ | ✅ | ✅ | works          | —             | A/H/C green via PexpectDriver (`1 passed in 211s`). |
| codex    | ✅ | ✅ | ✅ | works          | —             | Green; compaction request on the wire. |
| claurst  | ✅ | ✅ | `0` | works          | —             | A/H/`0` green via PexpectDriver (`1 passed in 301s`, 2026-07-27), once the x86_64 release artifact was published (see [windows-powershell.md](windows-powershell.md) → "claurst x86_64 release artifact"). WSL2 is x86_64, so the arch-aware installer selects `x86_64` and fetches `claurst-linux-x86_64.tar.gz` with no 404. `0` = compacts=False change-detector asserting `summarization_requests == 0`. |

## Runtime / environment notes (P3 prerequisites, plan §10.3)

- **`sbx` runs standalone in WSL2** — no Docker Desktop WSL integration needed. `/usr/bin/sbx`
  v0.34.0; `sbx login` is a **separate** auth session from Windows-native. `resolve_backend()` → `sbx`.
- **KVM access was the one environment blocker (fixed):** sbx's microVM needs `/dev/kvm`, but the
  user was not in the `kvm` group → `sailor: Hypervisor error: KVM error: Permission denied
  (os error 13)` on VM start. Fixed by `sudo usermod -aG kvm mike` + `wsl --terminate Ubuntu`.
  This is an **environment** step, not danno code.
- **P1.5 smoke inside WSL2 — PASS:** `sbx create` starts the VM; the workspace mounts **same-path**
  (`/home/mike/…`, no `/c/` translation — so `container_path()`'s `as_posix()` no-op is correct on
  Linux); `-e NAME` env-forward works; `host.docker.internal` resolves from the container
  (`fe80::1`). Egress `localhost:11455` reachable via `host.docker.internal:11455`.
- **Fast gate green in WSL2** (`ruff` + `mypy` + `pytest` 683 passed) — confirms the fix-in-lane
  changes (container_path, the `sys.platform` watchdog guard, UTF-8 writes, arch-aware claurst)
  have **no POSIX regression**.
