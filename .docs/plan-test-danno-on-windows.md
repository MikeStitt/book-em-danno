# Plan — test danno on Windows (WSL2, cmd, PowerShell) on a native x86 PC

**Date:** 2026-07-09 · **Status:** plan (no test run yet) · **Branch base:**
`docs-benchmark-grading-harness-fidelity` → `docs-plan-test-danno-on-windows`.

## Motivation

Python was chosen for danno partly to **run cleanly across Windows CLIs**, but danno
has **never been run on Windows** — dev and CI are macOS/Linux only. A code probe
(2026-07-09) found **zero `sys.platform`/`os.name` branches** in the source: danno is
written pure-POSIX with no Windows awareness. This plan tests that latent goal on a
**native x86 Windows 11 PC**, across **WSL2, cmd.exe, and PowerShell**, with the local
model served by **Ollama on the Mac over the LAN**.

Secondary payoff: a native-x86 Windows PC is also the best home for the **official
SWE-bench grade leg** (native x86 Linux containers via Docker Desktop's WSL2 backend — see
[`benchmark-grading-harness-fidelity.md`](benchmark-grading-harness-fidelity.md) §5e), so
validating Windows here unblocks that route too.

## Goal & success criteria

Produce a **portability report** that, for each of the three shells, classifies every
danno surface as **works / degrades / breaks**, with a root-caused, file-referenced,
prioritized **fix backlog**. "Runs cleanly" is defined per surface in the matrix below —
this is a **test/characterization** effort; **fixes are follow-ups**, not in scope here.

Concrete success bar for the session:
- **WSL2:** danno reaches parity with macOS for the read-only + config surfaces, and the
  run leg drives a model on the Mac's Ollama end-to-end (at least one `bench aider` cell
  passes). This is the expected "supported" path.
- **cmd + PowerShell:** every surface is *characterized* (even if it breaks); each break is
  root-caused to a specific line so we can decide support policy (§8 D1).

## Non-goals

- Fixing the portability breaks (separate branch once the report ranks them).
- Running Ollama on the PC (this plan pins Ollama on the Mac; PC-GPU option is §8 D2).
- The full SWE-bench sweep (only an optional single-instance native-x86 grade smoke, P5).

---

## Architecture under test — what runs where

| Component | Host | Notes |
|---|---|---|
| danno orchestrator (Python CLI) | **Windows PC** (per-shell: WSL2 / cmd / PowerShell) | the thing under test |
| run leg (harness + model loop) | **Docker Desktop sandbox on Windows** (Windows Hypervisor Platform; GA) | microVM is Linux inside → in-VM bash/heredocs unaffected |
| local model (Ollama) | **Mac, over the LAN** | `OLLAMA_HOST=0.0.0.0:11434`, Metal GPU; reached from the PC/sandbox by LAN IP |
| grade leg (optional, P5) | **WSL2 on the PC** (native x86 Linux containers) | official SWE-bench harness, native x86 |

Key architectural facts (from the companion doc):
- The **run leg** needs the model; the **grade leg does not** (grading is diff + tests).
- The sandbox reaches the model at hardcoded **`host.docker.internal:11434`**
  (`sandbox.py:50`), which on Windows resolves to the **Windows host, not the Mac** →
  a **repoint is mandatory** (§6).

---

## Grounded portability-hazard inventory (from the 2026-07-09 code probe)

Each hazard notes the expected behavior on **native** (cmd/PowerShell) vs **WSL2**.

### Shell / process
- **H1 — host `bash` invocation.** `commands/tools.py:107` runs
  `["bash", str(installer), "--local", "--no-fetch"]` **on the host**. Native Windows has
  no `bash` on PATH → **breaks** (`danno install` tool step). WSL2: **OK**.
- **H2 — `bash -lc` for sandbox exec.** `sandbox.py:1210`,
  `["docker","sandbox","exec",name,"bash","-lc",cmd]` — `bash` runs **inside the Linux
  microVM**, `docker` is the host binary (docker.exe) → **OK on all shells** *if* Docker
  Desktop's CLI is on PATH.
- **H3 — `shlex.join`/`shlex.quote`** used to build commands and log lines
  (`exec.py:161/173`, throughout). For commands sent **into the Linux sandbox** this is
  correct (POSIX target). Risk is only where a **Windows host path** (backslashes, spaces,
  drive letters) is interpolated into a sandbox command — audit those interpolations.

### Filesystem / permissions
- **H4 — `os.chmod(0o600)` on the secrets env-file.** `sandbox.py:693` (`p.chmod(0o600)`)
  plus every "chmod-600 env-file" for cloud auth (`run.py:180`, `baseline.py:64`,
  `bench.py:220/668`, …). On **native Windows** `os.chmod` only toggles the read-only bit —
  it does **NOT** enforce owner-only 0600 → **secret is effectively unprotected** (security
  finding, not just cosmetic). WSL2: **OK** (real POSIX perms). *Native fix later would need
  a Windows ACL path.*
- **H5 — temp paths.** `tempfile.mkstemp`/`mkdtemp` (`sandbox.py:690`, `tools.py:130`) are
  cross-platform (use `%TEMP%` on Windows) → **OK**, but the mkstemp file inherits H4's
  perms gap.
- **H6 — path separators / CRLF.** `pathlib` is portable; watch for any string path
  concatenation and for CRLF line-ending drift in generated files
  (`.opencode/opencode.jsonc`, env-files) if written on native Windows.

### Networking
- **H7 — hardcoded `host.docker.internal`.** `sandbox.py:50`, `capture/wiring.py:28`,
  `driver.py:141` (upstream host hardcoded; **only the port** is env via
  `DANNO_RELAY_UPSTREAM_PORT`). On Windows this points at the **Windows host**, not the Mac
  → the run leg can't reach the model without a repoint. Same on all three shells (it's a
  networking config, not a shell issue).
- **H8 — proxy-only egress allow-rule.** The sandbox egress allows `localhost:11434`
  (rewritten from `host.docker.internal`); a **remote Mac IP:11434** must be added to the
  allow-list, else the sandbox's CONNECT is rejected (memory:
  `sandbox-egress-and-process-lifetime`).
- **H9 — sampler/provenance `host_url`.** `commands/ollama.py` (`/api/tags`, `/api/ps`,
  `/api/show`) defaults to `localhost:11434` (`DEFAULT_HOST_URL`), parameterized in code but
  **not exposed as a CLI flag**. Under remote Ollama these must target the Mac → otherwise
  `--sample` VRAM + provenance silently go empty (degrades gracefully; not fatal).

### Toolchain / gate
- **H10 — dev toolchain.** `uv`, `ninja`, `ruff`, `mypy`, `pytest`, pre-commit hooks. All
  have Windows builds, but the **`ninja check` gate** and pre-commit hooks may assume
  bash/POSIX. Expected: **WSL2 OK**, native **needs characterization** (may need `uv run`
  invocation without the ninja wrapper).

**Takeaway hypothesis:** **WSL2 ≈ Linux → most things work** (only H7–H9 remote-Ollama
config needed). **cmd/PowerShell → H1, H4, H10 are the likely hard breaks**; read-only and
pure-Python surfaces (import, `--help`, config generation) likely work even there.

---

## Prerequisites & environment setup

**Mac (model server):**
1. `OLLAMA_HOST=0.0.0.0:11434 ollama serve` (bind LAN, not just loopback).
2. Open macOS firewall for 11434 on the local network; note the Mac's LAN IP (`MAC_IP`).
3. Pull the bench model(s) (e.g. `qwen3-coder-next`) on the Mac.

**Windows PC:**
4. Windows 11 x86, **Docker Desktop** installed (Sandboxes GA on Windows; WSL2 backend
   enabled), virtualization on in BIOS.
5. **WSL2** distro (Ubuntu) with Docker Desktop WSL integration enabled.
6. Python 3.x + **uv** installed in *each* environment to be tested (WSL2 shell; Windows
   Python for cmd/PowerShell).
7. Repo checkout + `uv sync` in each environment (this is itself part of the test — does the
   venv/lock resolve on Windows?).

**Reachability preflight (run in each shell before P-phases):**
8. `curl http://MAC_IP:11434/api/tags` (or PowerShell `Invoke-WebRequest`) — confirm the PC
   can see the Mac's Ollama over the LAN. This isolates LAN/firewall from danno bugs.

---

## Test matrix (surfaces × shells)

Record for each cell: **works / degrades / breaks**, exit code, first error, root-cause line.

| # | danno surface | invocation | WSL2 (expect) | cmd (expect) | PowerShell (expect) |
|---|---|---|---|---|---|
| S0 | import + `--help` | `python -m book_em_danno --help` / `uv run danno --help` | works | works | works |
| S1 | `danno doctor` | `danno doctor` (point at `MAC_IP`) | works | degrade (H9 host, H4 perms warn) | degrade |
| S2 | config generate | `danno` generate `.opencode/opencode.jsonc` (no side effects) | works | works (H6 CRLF watch) | works |
| S3 | `install --dry-run` | advise-only (prints commands) | works | degrade (H1 in advised text harmless) | degrade |
| S4 | `install --apply` (throwaway target) | executes tool installers | works | **break (H1 bash)** | **break (H1)** |
| S5 | `sandbox start` interactive | `danno sandbox start --harness claurst` → Mac Ollama | works* | characterize | characterize |
| S6 | `bench` aider (1 cell) | `danno bench --harness … --only <model>` | works* | characterize | characterize |
| S7 | `bench` swebench (1 inst, native-x86 grade) | P5 | works (WSL2 only) | n/a | n/a |

`*` gated on H7–H9 repoint being in place.

---

## Phased execution

**P0 — install & smoke (all three shells).** `uv sync`; `danno --help`; `python -m
book_em_danno --help`. Pass = danno imports and prints help in each shell. Captures whether
the venv/lockfile even resolves on Windows.

**P1 — doctor + remote-Ollama reachability.** LAN preflight (step 8), then `danno doctor`
pointed at the Mac. Characterize H9 (does doctor's Ollama check accept a remote host?).

**P2 — config generation (no side effects).** Generate `.opencode/opencode.jsonc` and env
overlays; diff against a macOS-generated reference (H6 CRLF / path drift). Pure-Python;
expected to pass on all shells.

**P3 — sandbox start + run leg vs Mac Ollama.** Apply the H7–H9 repoint (§6), launch an
interactive `sandbox start`, drive one prompt through a local model on the Mac. Pass = a
tool-calling turn completes against Mac Ollama. This is the crux for cmd/PowerShell.

**P4 — bench aider (the triple).** One `bench aider` cell (e.g. `qwen3-coder-next` on
`python/proverb`) end-to-end; grade must pass. Pass = a green cell + a `bench.json` row.
Run in WSL2 first (expected green), then attempt cmd/PowerShell (characterize).

**P5 — optional: native-x86 swebench grade (WSL2 only).** One Python instance
(django-16527), run leg via the sandbox, grade natively in WSL2's x86 Docker backend.
Confirms the native-x86 grade-box thesis (no emulation; #520 arm64 issues absent).

---

## Remote-Ollama plumbing (the H7–H9 repoint)

Two ways to point the sandbox's `host.docker.internal:11434` at the Mac:
- **(a) Host-side port-forward (no code change):** run a forwarder on the Windows host so
  `host.docker.internal:11434` → `MAC_IP:11434` (e.g. `socat`/`ssh -L` in WSL2, or a
  Windows port-proxy `netsh interface portproxy`). Keeps danno untouched — **preferred for
  a first test.**
- **(b) Code lever:** add an env/flag for the upstream **host** (today only the port is
  configurable — `driver.py:141`). Cleaner long-term; a follow-up, not this plan.

Plus: **proxy allow-rule** for `MAC_IP:11434` (H8), and if using `--sample`/provenance,
point `host_url` at the Mac (H9). Record which of these needed code vs config — that list
*is* part of the deliverable.

---

## Deliverables

1. `.docs/windows-portability-report.md` — the surfaces×shells matrix filled in, per-shell
   transcripts/exit codes, and a **prioritized, file-referenced fix backlog** (H1–H10 ranked
   by support-policy impact).
2. A recommendation on **support policy** (§8 D1).
3. If the run leg works: a `bench.json` row proving a Windows-driven, Mac-served cell.

---

## Decision points (owner: user)

- **D1 — Which shells does danno officially support?** Likely outcome: **WSL2 = supported**
  (Linux parity), **cmd/PowerShell = best-effort or explicitly unsupported** given H1/H4/H10.
  Decide before investing in native fixes.
- **D2 — Does the PC have an NVIDIA GPU?** If yes, a follow-up should test **Ollama native on
  the PC** (CUDA) — which moots the Mac and makes the PC a single-box danno host (native x86
  grade + local GPU model). This plan pins Ollama on the Mac regardless.
- **D3 — Repoint approach** (§6): host-side port-forward (test-now) vs a real upstream-host
  code lever (productionize). Start with (a).
- **D4 — Should CI gain a Windows/WSL2 job** once WSL2 parity is shown?

## Risks / open investigations

- **R1 — Docker sandbox × WSL2 coexistence (nested virt).** Both the sandbox microVM and
  WSL2 want the hypervisor; verify `docker sandbox` works through WSL integration (carried
  over as I5 from the companion doc).
- **R2 — Secret exposure on native Windows (H4).** If we ever "support" cmd/PowerShell, the
  chmod-600 guarantee must be replaced with a Windows-ACL path — do **not** ship cloud-auth
  benches on native Windows until then.
- **R3 — LAN latency** on Mac-served inference — expected negligible (Ollama streams) on
  wired/fast Wi-Fi; record tokens/s vs a local-Mac baseline.
- **R4 — Toolchain gate (H10)** — `ninja check` / pre-commit may need a POSIX shell; native
  Windows may only run `uv run pytest` directly.
