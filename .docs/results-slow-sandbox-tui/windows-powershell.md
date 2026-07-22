# Windows · PowerShell — slow-sandbox-tui results

date: 2026-07-22
host OS / version: Windows 11 Pro 10.0.26200 · shell: powershell · Python 3.14.2 (uv venv)
sbx v0.34.0 · pywinpty 3.0.5 · pyte 0.8.2 · driver: WinPtyDriver
fidelity: host-pty (real `sbx exec -it`, ConPTY via pywinpty)

| harness  | A | H | C | classification | leg           | notes / root-cause |
|----------|---|---|---|----------------|---------------|--------------------|
| opencode | ✅ | ✅ | ✅ | works          | —             | A/H/C green through WinPtyDriver first try. |
| codex    | ✅ | ✅ | ✅ | works          | —             | Green with the `model_auto_compact_token_limit` graft; compaction request on the wire. |
| claurst  | ❌ | — | — | breaks         | danno-product → **blocked on release artifact** | Install fails before the TUI: `claurst.py` hardcoded `claurst-linux-aarch64.tar.gz`, but the Windows sandbox VM is **x86_64** → the aarch64 binary can't exec. **Fixed in-lane** — install is now arch-aware (`uname -m`, commit `fix(claurst): select the release asset by container arch`). **Residual blocker (NOT danno code):** `MikeStitt/claurst` `v0.1.6-danno1` publishes only `claurst-linux-aarch64.tar.gz`; the x86_64 install now fails **loud with a 404** until a `claurst-linux-x86_64.tar.gz` build is published to that release. |

## Environment / runtime notes

- **Runtime available:** `sbx ls` resolves after `sbx login`; `resolve_backend()` → `sbx`
  (legacy `docker sandbox` is **removed** on this Docker build, so `sbx` is the only backend).
- **P1.5 smoke (danno-product frame) — PASS**, verified against live sbx:
  - `sbx create` direct-mounts a Windows workspace; the repo is reachable in the VM at the
    MSYS path `/c/Users/…` (drive `C:` → `/c`, backslashes → slashes).
  - `sbx exec -it -w /c/…` works; `-w C:/…` fails the OCI runtime `chdir`
    (`No such file or directory`). Drove the danno-product fix
    `fix(sandbox): render container paths for the Linux VM on Windows hosts` (`container_path()`).
  - `-e NAME` env-forward (issue #99) works; the mount is readable.
- **Egress:** `sbx policy allow network --sandbox <n> localhost:11455`; the VM reaches the
  host-side stub+capture proxy via `host.docker.internal:11455`. No LAN/Ollama needed
  (Ollama-free stub suite). `balanced` base policy also permits github.com (claurst CDN).
- **Driver read lever:** pywinpty 3.0.5's high-level `read()` blocks on its socket transport
  (the `PYWINPTY_BLOCK`/`read_blocking` path is commented out in this release); non-blocking
  reads use `fileobj.settimeout()` → `TimeoutError` == "no data this round". Pinned `==3.0.5`.
