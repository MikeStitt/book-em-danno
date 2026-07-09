# Plan — cross-platform testing of danno (Linux · macOS · Windows), live + CI

**Date:** 2026-07-09 · **Status:** plan (no test run yet) · **Branch base:**
`docs-benchmark-grading-harness-fidelity` → `docs-plan-test-danno-on-windows`.
(Renamed from `plan-test-danno-on-windows.md` when scope broadened to Linux + a CI matrix.)

## Motivation

Python was chosen for danno partly to **run cleanly across Windows CLIs**, but danno has
**never been run on Windows** — dev and CI are macOS/Linux only. A code probe
(2026-07-09) found **zero `sys.platform`/`os.name` branches**: danno is pure-POSIX with no
Windows awareness. This plan (1) tests danno live on a **native x86 Windows 11 PC** across
**WSL2, cmd.exe, PowerShell** with the model served by **Ollama on the Mac over the LAN**,
(2) adds **native Linux** as a first-class live target, and (3) expands **GitHub Actions**
to gate danno on **macOS + Windows + Linux**.

Secondary payoff: a native-x86 Windows/Linux box is also the best home for the official
SWE-bench grade leg (native x86 containers, no emulation — see
[`benchmark-grading-harness-fidelity.md`](benchmark-grading-harness-fidelity.md) §5).

## The organizing idea: TWO test tiers

The two asks live at different levels and must not be conflated:

| Tier | What it exercises | Needs | Where it runs |
|---|---|---|---|
| **Tier 1 — Live** | the **run leg + grade leg** (harness loop, model, sandbox, grading) | Docker Desktop sandbox + Ollama | **hardware-in-the-loop** (the PC + Mac; a Linux box) — **cannot** run on hosted CI |
| **Tier 2 — CI gate** | the **portable Python surface** (lint/format/type/unit) | just Python + uv | **GitHub Actions** hosted runners |

Tier 2 is where the H1–H10 portability hazards surface as unit failures; Tier 1 is where
the sandbox/Ollama-backed behavior is proven. Hosted runners have **no Docker Desktop
sandbox and no Ollama**, so the live tier stays manual — that division is deliberate.

---

# TIER 1 — Live / hardware-in-the-loop tests

## Goal & success criteria

Produce a **portability report** classifying every danno surface, per environment, as
**works / degrades / breaks**, with a root-caused, file-referenced fix backlog. This is
**characterization**; fixes are follow-ups.

- **WSL2 / native Linux:** parity with macOS on read-only + config surfaces, and the run
  leg drives a model end-to-end (≥1 `bench aider` cell passes).
- **cmd / PowerShell:** every surface *characterized* (even if broken); each break
  root-caused to a line, to decide support policy (D1).

## Live targets

| Target | Host | Model | Notes |
|---|---|---|---|
| **Windows PC** — WSL2 / cmd / PowerShell | native x86 Win 11 | **Mac Ollama over LAN** | the three-shell characterization |
| **Native x86 Linux** *(new)* | bare/VM Linux | **local Ollama if GPU**, else Mac over LAN | see caveats below |
| macOS *(baseline)* | this Mac | local Ollama | the current dev platform — reference for diffs |

**Native-Linux caveats (why it's not just "same as WSL2"):**
- **`docker sandbox` on Linux is roadmap / not yet GA** and needs **Docker Desktop** (most
  Linux boxes run Docker *Engine*). So the **run leg may be blocked on native Linux** until
  the sandbox ships — the **grade leg (Docker Engine) works today**. Record sandbox
  availability as the first Linux finding.
- **Linux/NVIDIA is danno's real bench target** (telemetry portability rule), so if the
  Linux box has an NVIDIA GPU, test **Ollama *local* on Linux** (the highest-fidelity
  bench env), not only remote-Mac.
- WSL2 vs native Linux side-by-side isolates **WSL-specific** quirks from generic Linux.

## Architecture under test

| Component | Host | Notes |
|---|---|---|
| danno orchestrator | Windows (WSL2/cmd/PS) · Linux · macOS | the thing under test |
| run leg (harness + model loop) | Docker Desktop sandbox (Win/Mac GA; **Linux roadmap**) | microVM is Linux inside → in-VM bash/heredocs unaffected |
| local model (Ollama) | Mac over LAN, **or** local (Linux w/ GPU, macOS) | `OLLAMA_HOST=0.0.0.0` when remote |
| grade leg (optional) | WSL2 / native Linux — native x86 Docker | official SWE-bench harness, no emulation, no Ollama |

## Grounded portability-hazard inventory (2026-07-09 code probe)

Native = cmd/PowerShell; WSL2/Linux = POSIX baseline (expected OK unless noted).

### Shell / process
- **H1 — host `bash` invocation.** `commands/tools.py:107` runs
  `["bash", str(installer), "--local", "--no-fetch"]` on the host → **breaks** native
  (no `bash`); WSL2/Linux OK. *(Caveat: GitHub Windows runners ship Git Bash, so CI may
  mask this — a clean Windows box won't have it.)*
- **H2 — `bash -lc` for sandbox exec.** `sandbox.py:1210` — `bash` runs **inside the Linux
  microVM**; `docker` is the host binary → OK on all shells if Docker CLI is on PATH.
- **H3 — `shlex.join`/`shlex.quote`.** Correct for commands targeting the Linux sandbox
  (POSIX target); note `shlex` emits POSIX quoting on **all** OSes, so string-compare tests
  won't spuriously differ. Risk: interpolating a **Windows host path** (backslash/drive)
  into a sandbox command — audit those sites.

### Filesystem / permissions
- **H4 — `os.chmod(0o600)` on secret env-files.** `sandbox.py:693` + every "chmod-600
  env-file" (`run.py:180`, `baseline.py:64`, `bench.py:220/668`). On **native Windows**
  `os.chmod` only toggles read-only — **secret is effectively unprotected** (security, not
  cosmetic). WSL2/Linux OK. *Native fix later = a Windows-ACL path.*
- **H5 — temp paths.** `tempfile.mkstemp/mkdtemp` (`sandbox.py:690`, `tools.py:130`)
  cross-platform (uses `%TEMP%`) → OK, but inherits H4's perms gap.
- **H6 — path sep / CRLF.** `pathlib` portable; watch string path-concat and CRLF drift in
  generated files (`.opencode/opencode.jsonc`, env-files) written on native Windows.

### Networking
- **H7 — hardcoded `host.docker.internal`.** `sandbox.py:50`, `capture/wiring.py:28`,
  `driver.py:141` (upstream host hardcoded; **only port** via `DANNO_RELAY_UPSTREAM_PORT`).
  On Windows/Linux this points at the **local** host, not the Mac → run leg needs a repoint
  (§ Remote-Ollama). Same across shells (networking, not shell).
- **H8 — proxy-only egress allow-rule.** A **remote Mac IP:11434** must be allow-listed or
  the sandbox CONNECT is rejected (memory `sandbox-egress-and-process-lifetime`).
- **H9 — sampler/provenance `host_url`.** `commands/ollama.py` (`/api/tags`,`/api/ps`,
  `/api/show`) defaults `localhost:11434` (`DEFAULT_HOST_URL`), not a CLI flag → under
  remote Ollama, `--sample`/provenance silently empty (degrades gracefully).

### Toolchain / gate
- **H10 — dev toolchain.** `uv`/`ninja`/`ruff`/`mypy`/`pytest`/pre-commit have Windows
  builds, but the **`ninja check` wrapper** and pre-commit hooks may assume bash/POSIX →
  WSL2/Linux OK, native needs characterization (may run `uv run pytest` directly). **This
  is also the Tier-2 blocker** (see CI ninja-install below).

**Hypothesis:** WSL2 ≈ Linux → most works (only H7–H9 remote-Ollama config). cmd/PowerShell
hard breaks = **H1, H4, H10**; pure-Python surfaces (import, `--help`, config-gen) likely
work even there.

## Prerequisites

**Model server (Mac, or local on Linux/mac):** `OLLAMA_HOST=0.0.0.0:11434 ollama serve`;
open firewall for 11434 on the LAN; note `MAC_IP`; pull bench model(s).
**Each client env (WSL2 / cmd / PowerShell / Linux):** Docker Desktop (Win/Mac) or Engine
(Linux); Python + **uv**; `uv sync` (does the lock resolve on Windows? — itself a test).
**Reachability preflight (per shell):** hit `http://MAC_IP:11434/api/tags` (curl / PS
`Invoke-WebRequest`) to isolate LAN/firewall from danno bugs.

## Live matrix (surfaces × environments)

Record: works / degrades / breaks, exit code, first error, root-cause line.

| # | surface | invocation | WSL2 | Linux | cmd | PowerShell |
|---|---|---|---|---|---|---|
| S0 | import + `--help` | `python -m book_em_danno --help` | works | works | works | works |
| S1 | `danno doctor` (→ `MAC_IP`) | `danno doctor` | works | works | degrade (H9,H4) | degrade |
| S2 | config generate (no side effects) | generate `.opencode/opencode.jsonc` | works | works | works (H6) | works |
| S3 | `install --dry-run` (advise-only) | prints commands | works | works | degrade | degrade |
| S4 | `install --apply` (throwaway) | runs installers | works | works | **break (H1)** | **break (H1)** |
| S5 | `sandbox start` interactive → model | `sandbox start --harness claurst` | works* | **sandbox-avail?** | characterize | characterize |
| S6 | `bench` aider (1 cell) | `bench --harness … --only <model>` | works* | works*/avail | characterize | characterize |
| S7 | `bench` swebench (1 inst, native-x86 grade) | P5 | works | works | n/a | n/a |

`*` gated on H7–H9 repoint. Linux S5/S6 gated on `docker sandbox` availability.

## Phased execution (Tier 1)

- **P0 — install & smoke** (all envs): `uv sync`; `danno --help`. Pass = imports + help.
- **P1 — doctor + remote-Ollama reachability**: LAN preflight, `danno doctor` → Mac.
- **P2 — config generation** (no side effects): diff generated files vs a macOS reference
  (H6 CRLF/path drift). Pure-Python; expected pass everywhere.
- **P3 — sandbox start + run leg vs model**: apply H7–H9 repoint, launch interactive
  sandbox, drive one prompt. Pass = a tool-calling turn completes. (Linux: first confirm
  `docker sandbox` exists.)
- **P4 — bench aider (the triple)**: one cell end-to-end + green grade + `bench.json` row.
  WSL2/Linux first (expected green), then cmd/PowerShell (characterize).
- **P5 — optional native-x86 swebench grade** (WSL2/Linux): one Python instance
  (django-16527), grade natively in x86 Docker — confirms the native-x86 grade thesis.

## Remote-Ollama plumbing (H7–H9 repoint)

Point the sandbox's `host.docker.internal:11434` at the Mac:
- **(a) Host-side port-forward, no code change** *(preferred first)*: forward
  `host.docker.internal:11434` → `MAC_IP:11434` (WSL2/Linux `socat`/`ssh -L`; Windows
  `netsh interface portproxy`).
- **(b) Code lever**: add an env/flag for the upstream **host** (today only port —
  `driver.py:141`). Follow-up.
Plus proxy allow-rule for `MAC_IP:11434` (H8) and sampler `host_url`→Mac (H9). Record which
needed **code vs config** — that list is part of the deliverable.

---

# TIER 2 — GitHub Actions CI expansion

## Current state (grounded)

`.github/workflows/check.yml` on push-to-main + PR:
- **matrix `os: [ubuntu-latest, macos-latest]`** → so **macOS and Linux are ALREADY
  covered.** The CI gap is **Windows only.**
- steps: checkout → `astral-sh/setup-uv` → **install ninja** (`shell: bash`, brew on macOS
  / apt otherwise) → `uv sync --locked --all-extras --dev` → **`ninja check`**.
- `ninja check` = `ruff check . && ruff format --check . && mypy && pytest -q -m "not slow"`
  (all `uv run`, all cross-platform Python tools).

## What CAN and CANNOT run in hosted CI

- **CAN:** the whole gate (lint/format/type + the **fast, non-`slow`** unit suite). This is
  precisely the surface where H1/H3/H4/H6/H10 either pass or reveal real bugs on Windows.
- **CANNOT:** the run leg (**no Docker Desktop sandbox** on hosted runners; Windows/macOS
  runners have no Docker at all by default) and any **Ollama**-backed bench. Those are
  `-m slow`/live and stay in **Tier 1**. Do not attempt them in `check.yml`.

## Proposed CI changes

### Change 1 — add Windows to the gate matrix
`os: [ubuntu-latest, macos-latest, windows-latest]` (optionally add **`ubuntu-24.04-arm`**
for arm-Linux coverage, since real benches run on Linux/NVIDIA which may be arm). Two
Windows enablers:

- **Ninja install must gain a Windows path.** The current `Install ninja` step's `bash`
  brew/apt branches don't cover Windows. **Recommended fix: install ninja portably via its
  PyPI wheel** (e.g. `uv tool install ninja`, or add `ninja` to dev deps) — this **removes
  the per-OS brew/apt/`shell: bash` branch entirely** and makes one step work on all three
  OSes. (Alt: `choco install ninja` in a Windows arm.)
- **Line endings:** add a repo `.gitattributes` (`* text=auto eol=lf`) so checkout on
  Windows doesn't CRLF-mangle files the fast suite reads (guards H6 in CI).

### Change 2 — a Windows shell-smoke job (the "cmd, PowerShell" ask)
Running the *gate* under different shells adds little (the Python tools behave identically);
the shell dimension matters at **danno invocation**. Add a Windows-only job that matrixes
the shell and runs a no-side-effect smoke:

```
strategy:
  matrix:
    shell: [cmd, pwsh, powershell, bash]   # pwsh=PS Core, powershell=Win PS 5.1, bash=Git Bash
steps:
  - run: danno --help            # + `python -m book_em_danno --help`, a config-gen dry run
    shell: ${{ matrix.shell }}
```

This characterizes entry-point resolution + arg quoting per Windows shell — where
cmd/PowerShell actually diverge — without needing Docker/Ollama.

### Change 3 — advisory first, required later
Land the Windows gate job with **`continue-on-error: true`** (advisory) until it's green,
then flip to a **required** status check. Don't block PRs on a brand-new, likely-red
Windows job on day one. macOS/Linux stay required throughout.

### Change 4 — fast-suite Windows triage (a deliverable, not a guess)
The first Windows run will fail some `not slow` tests. Triage each into:
- **(a) real portability bug** → fix on a follow-up branch (maps to an H-hazard), or
- **(b) legitimately POSIX-only test** → mark `@pytest.mark.skipif(sys.platform == "win32", reason=…)` + file an issue.
Likely (b) offenders: tests that spawn real `bash`/`docker`, assert `chmod`/perms, use
hardcoded `/tmp`, or compare path strings with `/`. (`shlex.join` output is POSIX on all
OSes, so those assertions are safe.)

## Sketch (illustrative, not final)

```yaml
jobs:
  gate:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]   # + ubuntu-24.04-arm (optional)
    runs-on: ${{ matrix.os }}
    continue-on-error: ${{ matrix.os == 'windows-latest' }}   # advisory until green
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv tool install ninja        # portable — replaces the brew/apt bash step
      - run: uv sync --locked --all-extras --dev
      - run: ninja check
  windows-shell-smoke:
    runs-on: windows-latest
    continue-on-error: true
    strategy:
      matrix: { shell: [cmd, pwsh, powershell, bash] }
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --locked --all-extras --dev
      - run: uv run danno --help
        shell: ${{ matrix.shell }}
```

---

## Deliverables

1. `.docs/windows-portability-report.md` — Tier-1 surfaces×envs matrix filled in, per-shell
   transcripts/exit codes, prioritized file-referenced fix backlog (H1–H10).
2. An updated `check.yml` (or a companion `check-windows.yml`) implementing Changes 1–3,
   plus the fast-suite triage output (Change 4) as skip-marks + issues.
3. A support-policy recommendation (D1) and a CI-required-status recommendation (D5).
4. If the run leg works: a `bench.json` row proving a Windows/Linux-driven, Mac-served cell.

## Decision points (owner: user)

- **D1 — Which shells/OSes does danno officially support?** Likely: **WSL2 + Linux + macOS
  = supported; native cmd/PowerShell = best-effort or unsupported** given H1/H4/H10.
- **D2 — GPUs?** Does the **Windows PC** or the **Linux box** have an NVIDIA GPU? If yes,
  test **Ollama local** there (moots the Mac; single-box native-x86 grade + local GPU model).
- **D3 — Repoint approach:** host-side port-forward (now) vs upstream-host code lever (later).
- **D4 — arm-Linux CI?** Add `ubuntu-24.04-arm` to the gate matrix?
- **D5 — Make the Windows gate a *required* check** once green, or keep advisory?

## Risks / open investigations

- **R1 — Docker sandbox × WSL2 nested virt** — verify `docker sandbox` works through WSL
  integration (I5 from the companion doc).
- **R2 — Secret exposure on native Windows (H4)** — do **not** ship cloud-auth benches on
  native Windows until a Windows-ACL replacement exists.
- **R3 — LAN latency** on Mac-served inference — expected negligible (Ollama streams);
  record tokens/s vs a local baseline.
- **R4 — Toolchain gate (H10)** — `ninja check` / pre-commit may need a POSIX shell natively.
- **R5 — Windows CI runner masks H1** — Git Bash is present on `windows-latest`, so the
  host-`bash` break won't show in CI; only a clean box (or the shell-smoke `cmd`/`pwsh`
  cells) exposes it. CI green ≠ clean-Windows green.
- **R6 — Linux run-leg blocked** until `docker sandbox` GA on Linux; grade leg works now.
