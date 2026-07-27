# Windows · PowerShell — slow-sandbox-tui results

date: 2026-07-22
host OS / version: Windows 11 Pro 10.0.26200 · shell: powershell · Python 3.14.2 (uv venv)
sbx v0.34.0 · pywinpty 3.0.5 · pyte 0.8.2 · driver: WinPtyDriver
fidelity: host-pty (real `sbx exec -it`, ConPTY via pywinpty)

| harness  | A | H | C | classification | leg           | notes / root-cause |
|----------|---|---|---|----------------|---------------|--------------------|
| opencode | ✅ | ✅ | ✅ | works          | —             | A/H/C green through WinPtyDriver first try. |
| codex    | ✅ | ✅ | ✅ | works          | —             | Green with the `model_auto_compact_token_limit` graft; compaction request on the wire. |
| claurst  | ✅ | ✅ | `0` | works          | —             | A/H/`0` green through WinPtyDriver (`1 passed in 249s`, 2026-07-27). Previously blocked: `claurst.py` hardcoded `claurst-linux-aarch64.tar.gz` but the sandbox VM is **x86_64**; **fixed in-lane** to be arch-aware (`uname -m`, commit `fix(claurst): select the release asset by container arch`). The residual gap — the release shipped only the aarch64 asset — is **now closed**: `claurst-linux-x86_64.tar.gz` was built + published to `MikeStitt/claurst` `v0.1.6-danno1` (provenance below). The installer selects `x86_64`, fetches with no 404, and A/H/`0` pass. `0` = compacts=False change-detector asserting `summarization_requests == 0`. |

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

## claurst x86_64 release artifact (2026-07-27) — blocker closed

The one previously-red cell was **not a danno bug** (danno was already arch-aware and failed
loud with a 404); it was a **missing release artifact**. Built and published per
`.docs/2026-07-27-windows-claurst-x86_64-build-handoff.md`:

- **Built from:** `MikeStitt/claurst` `danno-integration` tip `dafbde1` (source-identical to the
  release commit `d466ec4` the aarch64 asset was cut from). Native amd64 build in a clean
  `rust:1-bookworm` container (`cargo build --release --locked -p claurst`, then `strip`) — no
  QEMU, ~9 min. `file` → `ELF 64-bit LSB pie executable, x86-64 … stripped`; `--version` → the
  bare `claurst 0.1.6`.
- **Published asset:** `claurst-linux-x86_64.tar.gz` (single `claurst` at tar root, mode `0755`),
  **added** to the existing `v0.1.6-danno1` release via `gh release upload` (never `release.yml`).
  Size `14583484`, `sha256:fcf9e047a37b3316074ee7a070ceda22b23ecd876dd14c74bd63667b4a769d08`.
- **aarch64 asset untouched** (still `sha256:8c95dada…`, 2026-06-27); npm registry + release notes
  unchanged.
- **Re-run result:** claurst A/H/`0` green on Windows-PowerShell, Windows-cmd, and WSL2 — the
  arch-aware installer selects `x86_64` and fetches with no 404. See the sibling result files.
