# Plan — `@slow` sandbox TUI tests (interactive launch), cross-platform

**Date:** 2026-07-21 · **Status:** plan (spikes done, no test committed) · **Author:**
design-of-record.

**Driver decision (2026-07-21, user):** use **host-pty on both OSes** — `pexpect` on POSIX,
`pywinpty` on Windows — so *every* platform drives the real `sbx exec -it` frame. tmux is
demoted to a documented fallback (§3.1). **Execution is split across two Claude Code instances
on two machines** (this one on macOS; a second on the Windows PC), with WSL2 folded into the
Windows instance — see **§10 Work division** for the branch/ownership/results contract.

Companion to [`research-tui-interactive-testing.md`](research-tui-interactive-testing.md)
(the empirical spike results, §7.1/7.2/7.3/8.1) and
[`plan-test-danno-cross-platform.md`](plan-test-danno-cross-platform.md) (the H1–H10
portability hazards + two-tier model). This plan turns the proven spikes into a committed,
cross-platform `@slow` suite.

---

## 1. Motivation & definition of done

danno's product claim is the **triple**: harness × model+config × sandbox, driven
**interactively** the way a human runs `danno sandbox start`. Today nothing in the test set
exercises the interactive launch path end-to-end — the slow suite drives `danno bench`
(non-interactive) and the fast suite mocks the runner. The spikes (2026-07-21) proved we
*can* drive the real interactive TUI of **opencode, codex, and claurst** through a pty and
assert against the captured HTTP wire. This plan commits that as a maintained suite.

**Explicitly in scope:** opencode, codex, claurst. **Out of scope:** Claude Code (its own
auth/model path; excluded by standing decision). Real-AI leg = **stub AI only** for the
committed tests (deterministic, GPU-free, seconds not minutes); a local-Ollama-qwen variant
is a documented manual escalation, not a gated row.

**Done =** for each in-scope harness, a `@slow`-marked, `sandbox`-marked test that, on each of
**Linux, macOS, WSL2, Windows-cmd, Windows-PowerShell**, **runs green** — and where a platform
break is found, we **fix its root cause in-lane** (in the driver *or* in danno's own code) so the
test+platform combination actually works, rather than deferring it. A **loud skip** is reserved
for a genuine **runtime absence** (Docker Desktop / sbx unsupported on that build — not danno's
code to fix); the test never silently passes without exercising the harness, and never weakens an
assertion to force green. Each test proves three legs:

| Leg | Name | Proves | Wire assertion |
|---|---|---|---|
| **A** | reaches TUI | the harness's interactive code path opened (banner + composer) | — (screen markers) |
| **H** | turn on wire | typing a prompt drives one model turn; stub reply rendered | `completion/responses_requests ≥ 1` |
| **C** | compaction | inflated `usage` drives (or provably does not drive) auto-compaction | `summarization_requests` branched on `harness.compacts` |

The acceptance bar is deliberately low (per prior agreement): exercise the interactive code,
show the AI connected, reach a compaction decision with a fake AI, do a turn. We assert
against the **wire** (captured JSONL), not the ANSI paint — "wire, not paint."

---

## 2. The three legs & acceptance shape

Recap of what the spikes locked (detail in `research-tui-interactive-testing.md`):

- **A — reach TUI.** Launch the *real* danno frame, wait through first-run dialogs
  (codex trust `1`+Enter; claurst "Keyboard Shortcuts 2/2" ESC; opencode late auto-update
  modal ESC), assert banner + composer glyph on the rendered screen. Composer markers per
  harness: codex `/model to change`, opencode `ask anything`/`build ·`, claurst `❯` (U+276F).
- **H — turn on wire.** `submit()` a `?`-free prompt (`list the files here`), confirm a *new*
  request landed on the capture proxy, assert the stub's canned reply rendered. Assert
  `requests ≥ 1`.
- **C — compaction.** One-shot inflate the stub's `usage` on the *first* reply only (inflating
  every reply → unbounded compaction runaway: opencode 1184 reqs/592 summ in 67s), drive 2–3
  more turns, then assert on `summarization_requests` **branched by capability** (see §5).

**Per-harness C expectation (from spikes):**
- codex → `CONTEXT CHECKPOINT COMPACTION` request; needs top-level
  `model_auto_compact_token_limit` (small) in config.toml. `compacts=True`.
- opencode → `create a new anchored summary…` request + footer `156%→0%`. `compacts=True`.
- claurst → **no compaction even at 2M tokens** (usage flows, but auto-compact isn't armed
  in `v0.1.6-danno1`). `compacts=False` → assert `== 0` as a **change-detector**.

---

## 3. The cross-platform crux

The five named platforms differ in exactly **one** axis that matters here: **how a host process
obtains a pseudo-terminal** to drive `sbx exec -it`.

- **`pexpect` is Unix-only** — it needs Unix ptys (`os.openpty`, `termios`). It runs on
  **Linux, macOS, and WSL2** (WSL2 is Linux). It does **not** run on Windows-native Python.
- **Windows-native** (cmd + PowerShell share the same Windows Python interpreter) needs
  **ConPTY**, reachable from Python via **`pywinpty`** (`winpty.PtyProcess`).

So the 5 platforms **collapse to 2 host-pty backends**: POSIX (Linux/macOS/WSL2) and
Windows-native (cmd/PowerShell). Everything *else* in the harness is already portable:

- the **capture proxy** (`tests/slow/capture_proxy.py`) — pure `http.server` + `threading`;
- the **stub AI** (`book_em_danno.stubai`) — pure Python HTTP;
- **`pyte`** (VT emulator) — pure Python;
- the **in-container argv** (`bash -lc '…'`, heredocs) — always runs **Linux inside the VM**
  regardless of host (the microVM is Linux; H2 in the cross-platform plan);
- the **sbx CLI seam** (`sandbox_cli.base()` → `['sbx']` or `['docker','sandbox']`) — resolved
  by `shutil.which`, portable.

### 3.1 Decision: symmetric host-pty (pexpect + pywinpty), tmux as fallback

The plan uses **two host-pty driver implementations behind one `TuiDriver` protocol**, picked by
platform. **Both drive danno's real `sbx exec -it <argv>` frame** — the only difference between
them is which library owns the host pty:

1. **`PexpectDriver` — POSIX (Linux, macOS, WSL2).** Host `pexpect` spawns the real
   `sbx exec -it -w … <name> <argv>` under a Unix pty; bytes fed to `pyte`. This is the
   spike-proven path (all A/H/C green on macOS).

2. **`WinPtyDriver` — Windows-native (cmd, PowerShell).** Host `pywinpty` (`winpty.PtyProcess`,
   ConPTY backend) spawns the **same** `sbx exec -it … <argv>` under a ConPTY; bytes fed to the
   same `pyte`. Same protocol, same primitives — see §3.4 for the verified API mapping.

Because both run the identical launch command and only swap the host-pty library, **Windows is
the faithful peer of POSIX** (§3.2), not a lower-fidelity stand-in. `pywinpty` is now a
first-class dependency (Windows-only marker, §7).

**tmux-in-VM is the documented fallback, not the primary.** If a host-pty backend proves
unworkable on some platform (e.g. `pywinpty`+ConPTY can't drive sbx on a given Windows build),
`TmuxDriver` — plain `sbx exec <name> tmux send-keys/capture-pane`, no host pty — is the
escape hatch: it still gives the harness a real pty (allocated by tmux *inside* the VM), so the
model-wire legs (A/H/C) still hold, but it does **not** run danno's real launch command (§3.2).
It is invoked only via `make_driver(prefer="tmux")` and never silently: a run that falls back
records which fidelity level it used. No platform is claimed green on a fallback without saying so.

### 3.2 Fidelity: how each driver compares to a real `danno sandbox start`

What a **human** runs is one command and one tty chain:

```
human terminal ── sbx exec -it -w <target> -e OPENAI_API_KEY <name> bash -lc '…harness…'
                        │
                        └─ host allocates the pty · sbx plumbs -it into the VM · harness gets that tty
```

The three drivers sit at different distances from that:

| | **Human (production)** | **`PexpectDriver` (POSIX)** | **`WinPtyDriver` (Windows)** | **`TmuxDriver` (fallback)** |
|---|---|---|---|---|
| Command danno actually builds (`_exec_session`) | run verbatim | **run verbatim** | **run verbatim** | **not run** — `sbx exec <name> tmux new-session '<argv>'` |
| `-it` / host pty | terminal owns it | **pexpect owns it** (Unix pty) | **pywinpty owns it** (ConPTY) | **absent** — plain non-interactive `sbx exec` |
| Where the harness's tty comes from | host pty → sbx `-it` → VM | host pty → sbx `-it` → VM | host pty → sbx `-it` → VM | **tmux allocates it inside the VM** |
| `-e NAME` env forwarding (issue #99 path) | exercised | **exercised** | **exercised** | re-threaded onto the tmux child — *different plumbing* |
| resize host→VM (SIGWINCH / ResizePseudoConsole) | exercised | exercised (`setwinsize`) | exercised (`setwinsize`) | not exercised (tmux owns geometry) |
| Process-exit / EOF of the real frame (opencode update-restart EOF seen here) | exercised | **exercised** | **exercised** | not exercised (tmux session outlives it) |
| Harness interactive branch (`isatty`) | yes | yes | yes | **yes** (tmux pty) |
| **Model wire** (turns, compaction) — *the thing under test* | — | **identical** | **identical** | **identical** |

Read the table as: **both host-pty drivers differ from the human at exactly one point** — the
pty library (`pexpect` / `pywinpty`) stands in for the human's terminal emulator; the *command,
the flags, and the whole host→VM tty chain are byte-for-byte production* on each OS. **`TmuxDriver`
differs at two points** — (1) danno's real `sbx exec -it <argv>` is never issued; a `tmux
new-session` wrapper production never uses is issued instead, and (2) the harness's terminal is
born *inside* the VM from tmux rather than delivered *through* sbx's `-it` from the host. All
three prove the model-wire legs (A/H/C) equally; only the host-pty drivers also prove the real
launch frame — which is why they are primary on both OSes and tmux is only the fallback.

### 3.3 Wire assertion is host-side and portable

Both drivers assert against the **same** capture JSONL written by the host-side proxy. The VM
reaches the proxy via `host.docker.internal:<PROXY_PORT>` (H7); egress policy allows **only**
`localhost:<PROXY_PORT>` (never `**` — standing security invariant). On Linux/WSL2 the
`host.docker.internal` resolution inside the microVM is provided by the sandbox runtime (same
as macOS); if a target lacks it, the test **skips loudly** rather than falling back to a
weaker egress.

### 3.4 `WinPtyDriver` contract (verified — this is the Windows implementation spec)

The Windows driver is `pywinpty`. We verified its API against **pexpect 4.9.0** (installed) and
**pywinpty `main`** source (`winpty/_winpty.pyi`, `winpty/ptyprocess.py`) so the Windows Claude
implements against facts, not assumptions.

**The parts we'd use have near-parity.** pywinpty's high-level `PtyProcess` is modeled on
`ptyprocess.PtyProcess` (the transport layer *under* pexpect), not on pexpect's matcher — which
is fine because our design feeds bytes to `pyte` and matches the rendered **screen**, never
`expect()`. So the one headline pexpect feature pywinpty lacks (a regex `expect()` engine) is
one we don't use. `spawn(argv, cwd, env, dimensions=(rows,cols))`, `setwinsize(rows,cols)`,
`isalive()`, `terminate(force=)`, `close()`, and "send a key" (raw `write('\x1b')`/`write('\r')`)
all map across both backends. The `TuiDriver` protocol (`start/screen/send/enter/alive/close`)
sits above the only divergent internal — the read loop.

**Non-blocking reads exist on both — different ergonomics, not a missing capability.** (Earlier
draft wrongly said pywinpty had no non-blocking read; corrected here.)
- pexpect: per-call `read_nonblocking(size=1, timeout=-1)` — raises `pexpect.TIMEOUT`/`pexpect.EOF`;
  bytes when `encoding=None`.
- pywinpty: timing is set by **mode/construction**, not a per-call `timeout=` kwarg. Verified:
  low-level `PTY.read(self, blocking: bool = False)` (non-blocking **by default**); constructor
  `PTY.__init__(..., timeout: int = 30000)` (ms); high-level `PtyProcess` reads `PYWINPTY_BLOCK`
  env (`bool(int(os.environ.get('PYWINPTY_BLOCK', 1)))`, default blocking) and — in current
  releases — reads via a **socket** (`self.fileobj.recv(size)`), so `fileobj.settimeout()` is a
  further lever. So a background reader thread is **not** required.

**The real, bounded ConPTY costs** (what P4 signs up for):
1. **A version pin.** The read internals are in flux (the `# self.pty.read(size, blocking=…)`
   line is commented out in favor of the socket transport; pywinpty has moved winpty-C → ConPTY →
   Rust `winpty-rs` → socket across releases). Relying on `PYWINPTY_BLOCK`/`fileobj` pins a
   specific pywinpty version — this is the concrete meaning of "unexercised by us," not fidelity.
2. **str-only vs. bytes.** pywinpty hands back UTF-8 `str` (decode owned by the lib); pexpect with
   `encoding=None` gives raw bytes we can feed to `pyte.ByteStream` (partial-multibyte-safe). Minor
   robustness delta on the Windows side.
3. **Below the API: ConPTY's byte stream differs** from a Unix pty (more repaint/cursor sequences,
   reflow on resize), so identical API calls can yield a different *rendered* screen. Our
   match-on-stable-markers-after-settle absorbs most of it, but it's why ConPTY needs its own
   validation pass, not just a code port.

Net: `WinPtyDriver` is `PtyProcess.spawn` + a non-blocking read loop (via the flags above) +
`setwinsize`, with every proven primitive (`submit`, settle-ESC, one-shot-inflate) unchanged on
top. The bounded costs above (version-pin + a validation pass, **not** fidelity or a missing
non-blocking read) are what the Windows Claude signs up for. **Pin the exact `pywinpty` version**
in `pyproject.toml` and record it in the Windows results (§10).

---

## 4. `TuiDriver` architecture

A tiny protocol so the tests are written once and run on either backend:

```
# tests/slow/tui/driver.py  (sketch — NOT to be written until plan approval)

class TuiDriver(Protocol):
    def start(self) -> None: ...              # launch the harness frame
    def screen(self) -> str: ...              # current rendered screen text
    def send(self, keys: str) -> bool: ...    # type text / control keys; False if dead
    def enter(self) -> bool: ...              # submit
    def alive(self) -> bool: ...
    def close(self) -> None: ...

def make_driver(harness, name, target, env, *, prefer="auto") -> TuiDriver:
    # prefer="auto": PexpectDriver on POSIX, WinPtyDriver on Windows (host-pty, faithful).
    # prefer="tmux": force the tmux-in-VM fallback (records reduced fidelity).
    # Skips loudly if the platform's host-pty backend can't import / drive sbx.
```

- **`PexpectDriver`** (POSIX) — `pexpect.spawn` of the real `sbx exec -it …` argv, `encoding=None`
  (raw bytes) → `pyte.ByteStream`/`pyte.Screen`; `screen()` renders the pyte display;
  `send()`/`enter()` write to the pty; read loop uses `read_nonblocking(size, timeout)`. Import-
  guarded (Unix-only).
- **`WinPtyDriver`** (Windows) — `winpty.PtyProcess.spawn` of the **same** `sbx exec -it …` argv,
  `dimensions=(rows,cols)` → decoded `str` → same `pyte.Screen`; `send()`/`enter()` = `write`;
  read loop is non-blocking via the §3.4 levers. Import-guarded (`sys_platform == 'win32'`).
- **`TmuxDriver`** (fallback only) — `sbx exec <name> tmux new-session -d -x 200 -y 50 -s danno
  '<argv>'`; `screen()` = `capture-pane -p`; `send()`/`enter()` = `send-keys`. No host pty, no
  pyte. Reached only via `prefer="tmux"`; requires tmux in the VM (§7).

**All three** consume the identical `submit()`/settle-ESC/one-shot-inflate helpers (§6) — those
operate on the `TuiDriver` interface, not on any pty library directly, so they're backend-agnostic.
**Freezing this `TuiDriver` protocol is the handoff artifact** the Windows Claude builds against
(§10).

The **container argv** is produced by the *real* danno code path (`book_em_danno.commands.
sandbox._exec_session` / `launch`), captured once via the spike's `_CaptureLaunchRunner`
pattern, so both drivers launch exactly what a human's `danno sandbox start` would — only the
outer tty owner differs.

---

## 5. `compacts` capability flag

Compaction is a **per-harness capability**, not a universal assertion (spike proof: claurst
never compacts at 2M tokens). Add one optional field to the frozen `Harness` value object,
in the OPTIONAL-defaulted block next to `capture_via_relay`
(`src/danno_validator/harnesses/__init__.py:144`):

```
# Whether this harness performs usage-driven auto-compaction. When True, the interactive
# C-leg test asserts a summarization request appears on the wire under inflated usage; when
# False it asserts NONE appears — a CHANGE-DETECTOR. If a False harness ever starts
# compacting, that test goes RED loud, forcing a conscious flip to True + changelog + bump.
compacts: bool = True
```

- codex, opencode, claude, (default) → `compacts=True`.
- **claurst** (`harnesses/claurst.py`, the `Harness(...)` at line 71) → **`compacts=False`**.

**C-leg branch (in the parametrized test):**

```
if harness.compacts:
    assert wire.summarization_requests >= 1     # codex/opencode
else:
    assert wire.summarization_requests == 0      # claurst: change-detector
```

No extra precondition ceremony (that's hair-splitting — user, 2026-07-21). The **same test's
H leg** already proves turns happened; a broken flow fails H and can't manufacture a false
compaction. The stub still feeds the inflated round to claurst, so a *future* armed compaction
**would** trip the `== 0`.

---

## 6. Reusable primitives (proven in spikes)

Lifted from `scratchpad/spike_harness.py` / `spike_ac_codex.py`, rewritten against `TuiDriver`:

- **`settle_and_dismiss(driver, cfg, rounds=6)`** — pump the screen ~2.5s/round; for each known
  first-run modal (`dialogs=[(marker, key)]`) send its dismiss key (usually ESC). Handles the
  **late** opencode auto-update modal (appears *seconds after* the composer; ESC=Skip; **never
  Enter** — Enter confirms the update, restarts the process, EOFs the pty). Never send Enter
  while a modal could be up.
- **`submit(driver, cfg, wire, text, tries=3)`** — the wire-confirmed input primitive:
  `settle_and_dismiss` → type `text` + Enter → **confirm a new request appeared on the capture
  proxy** → retry if a racing overlay ate the Enter (mandatory for claurst's onboarding
  overlay). Returns the post-submit wire delta.
- **`one_shot_inflate(engine)`** — monkeypatch `ScriptEngine.next_reply` to inflate
  `prompt_tokens` **only on the first reply** (`_state["n"] == 1`), so the harness sees one
  over-budget turn and decides to compact, without runaway.
- **Per-harness config dict** (`HARNESS` in the spike) — banner/composer/dialog/summ markers +
  `inflate` magnitude — becomes a small table keyed by harness name in the test module.

---

## 7. Marker, skip guards, dependencies, provisioning

**New pytest marker.** Add to `pyproject.toml` `[tool.pytest.ini_options].markers`:

```
"sandbox: interactive TUI tests that launch a real sandbox harness and drive its pty",
```

Tests are marked `pytestmark = [pytest.mark.slow, pytest.mark.sandbox]`. `slow` keeps them out
of the fast gate; `sandbox` lets `-m sandbox` select just these.

**Skip guards (loud, never silent):**
1. `sandbox_runtime_down()` (existing, `tests/slow/sandbox_runtime.py`) — probes the *resolved*
   backend (`sbx ls`), skips if the runtime is down. (Memory: probe the resolved runtime, not
   standalone `docker info`.)
2. **Driver availability** — `make_driver(prefer="auto")` skips with a reason if the platform's
   host-pty backend can't import or can't drive sbx (POSIX: `pexpect` missing; Windows: `pywinpty`
   missing or ConPTY unavailable, e.g. Windows < 10 1809). The skip message names the platform and
   the missing piece; it never silently downgrades to tmux — a tmux run happens only when a human
   passes `prefer="tmux"`.
3. **tmux-in-VM present (fallback only)** — guard consulted only when `prefer="tmux"`; skip if tmux
   can't be installed in the VM.
4. **Per-harness install** — reuse `provisioned_sandbox()` (`tests/slow/gates_fixtures.py`)
   which generates config + provisions with `allow_hosts=(f"localhost:{PROXY_PORT}",)`.

**Dependencies (`pyproject.toml` `[dependency-groups].dev`).** Host-pty on both OSes, so `pyte`
is needed everywhere; the pty library is platform-split:

```
"pyte>=0.8",
"pexpect>=4.9  ; sys_platform != 'win32'",
"pywinpty>=2.0 ; sys_platform == 'win32'",   # PIN the exact version once P2 confirms it (§3.4)
```

**Provisioning tmux (fallback only).** A helper that `apt-get install -y tmux` inside the VM
(proxy-aware; memory `sandbox-pip-install-works`) is added but exercised only on the tmux
fallback path; the primary host-pty path needs nothing in the VM.

**No cross-machine dependency for the stub suite.** The stub AI + capture proxy are pure Python
and run on **whatever host drives the test** — the Windows PC runs its own stub+proxy locally
(no GPU, no Mac needed); the VM reaches it via `host.docker.internal:<PROXY_PORT>`. The
LAN-to-Mac-Ollama path is only for the optional real-model escalation (Q1), never the gated suite.

**conftest note.** `tests/conftest.py` already delenvs `DANNO_SANDBOX_CLI` for slow tests so
they use auto-detected sbx; do **not** add a second top-level conftest (it would shadow
`RecordingRunner`). New fixtures live in `tests/slow/tui/` and are imported by path like the
existing `gates_fixtures`.

---

## 8. File layout

```
tests/slow/tui/
  __init__.py
  driver.py            # TuiDriver protocol + PexpectDriver + WinPtyDriver + TmuxDriver + make_driver
  primitives.py        # settle_and_dismiss, submit, one_shot_inflate, HARNESS config table
  fixtures.py          # provisioned sandbox, capture proxy + stub wiring, wire metrics
  test_tui_launch.py   # the parametrized A/H/C test over [opencode, codex, claurst]
.docs/results-slow-sandbox-tui/
  README.md            # the results template + rollup matrix (§10)
  macos.md · wsl2.md · windows-cmd.md · windows-powershell.md   # one per platform×shell run
```

- Reuse (don't fork): `tests/slow/capture_proxy.py`, `tests/slow/sandbox_runtime.py`,
  `tests/slow/gates_fixtures.py` (`provisioned_sandbox`, `scripted_backend`, ports/`MODEL_TAG`),
  `book_em_danno.stubai`.
- **Ownership:** `driver.py` `PexpectDriver` + everything else in `tests/slow/tui/` is the macOS
  Claude's; `driver.py` `WinPtyDriver` is the Windows Claude's (built against the frozen protocol).
- Source change: `Harness.compacts` field + claurst override (§5) — the **only** `src/` edit for P1.

### 8.1 `driver.py` — the frozen contract (implementation spec)

The **exact** `TuiDriver` surface the Windows Claude builds against — freezing it is the P1 exit
gate. All coordinates are (rows, cols); the screen is a `pyte.Screen(COLS, ROWS)` with `COLS=160,
ROWS=48` (the spike geometry). `screen()` returns `"\n".join(screen.display).rstrip()`.

```python
COLS, ROWS = 160, 48

@runtime_checkable
class TuiDriver(Protocol):
    def start(self) -> None: ...            # spawn `sbx exec -it … <argv>` under a host pty
    def pump(self, seconds: float, want: Sequence[str] | None = None) -> bool: ...
                                            # feed pty→pyte for `seconds`; True early if any
                                            # `want` marker (lower-cased substring) is on screen
    def screen(self) -> str: ...            # current rendered screen text
    def send(self, keys: str) -> bool: ...  # write text/control bytes; False if the child is dead
    def enter(self) -> bool: ...            # convenience: send("\r")
    def alive(self) -> bool: ...
    def close(self) -> None: ...            # force-terminate; never raise

def make_driver(argv: list[str], env: dict[str, str], *, prefer: str = "auto") -> TuiDriver:
    # "auto": PexpectDriver on POSIX (os.name == "posix"), WinPtyDriver on Windows.
    # "tmux": force the in-VM fallback (records reduced fidelity).
    # Raises DriverUnavailable(reason) — the caller turns that into pytest.skip — when the
    # platform's host-pty lib can't import or ConPTY is absent. NEVER silently downgrades.
```

- **`PexpectDriver`** (POSIX, macOS/Linux/WSL2 — the P1 deliverable). Constructor takes the
  already-resolved `(exe, args, env)`. `start()` = `pexpect.spawn(exe, args=args, env=env,
  encoding=None, dimensions=(ROWS, COLS), timeout=120)` + a fresh `pyte.Screen`/`pyte.ByteStream`.
  `pump()` loops `read_nonblocking(8192, timeout=0.4)`, swallowing `pexpect.TIMEOUT` (→ `b""`) and
  treating `pexpect.EOF` as "stop, return current hit-state" (EOF is *expected* on quit and on the
  opencode update-restart — never an exception the test sees). `send()` wraps `child.send` in the
  spike's `safe_send` (guards `isalive()`, catches `OSError`). `close()` = `sendcontrol('c')`×2 +
  `send('q')` best-effort then `child.close(force=True)`, all exception-swallowed. `encoding=None`
  → **bytes** into `pyte.ByteStream` (partial-multibyte-safe; §3.4).
- **`WinPtyDriver`** (Windows — **P1 lands a stub** `raise NotImplementedError("P2: pywinpty",)`;
  filled in at P2 per §3.4). Same protocol; `winpty.PtyProcess.spawn(argv, env=env,
  dimensions=(ROWS, COLS))`, decoded `str` fed to `pyte.Screen` (a `pyte.Stream`, not `ByteStream`),
  non-blocking read via the §3.4 levers.
- **`TmuxDriver`** (fallback only, `prefer="tmux"`). `sbx exec <name> tmux new-session -d -x COLS
  -y ROWS -s danno '<argv>'`; `screen()` = `capture-pane -p`; `send()`/`enter()` = `send-keys`.
  No host pty, no pyte. Records reduced fidelity (§3.2). Not written in P1 unless a platform needs it.

### 8.2 `primitives.py` — harness table + wire-confirmed input

Direct lift of the spike helpers, rewritten against `TuiDriver` (they call `driver.pump/screen/
send`, never a pty lib). Contents:

- **`HARNESS: dict[str, HarnessTui]`** — one row per harness (a small frozen dataclass), lifted
  verbatim from `spike_harness.py`'s `HARNESS` dict:
  - `codex`: `model="stub"`, `inflate=50_000`, `wire_path="/responses"`, `banner=("welcome to
    codex","codex")`, `composer=("/model to change",)`, `dialogs=[("do you trust","1\r")]`,
    `summ_markers=("context checkpoint compaction","create a handoff summary","produced a summary
    of its thinking")`. Needs the top-level `model_auto_compact_token_limit` config graft (§8.3).
  - `opencode`: `model="stub"`, `inflate=50_000`, `wire_path="/chat/completions"`,
    `banner=("ask anything","fix broken tests")`, `composer=("ask anything","build ·")`,
    `dialogs=[("update","\x1b")]` (**ESC = Skip; never Enter**), `summ_markers=("create a new
    anchored summary from the conversation history","anchored summary")`.
  - `claurst`: `model="ollama/stub"`, `inflate=2_000_000`, `wire_path="/chat/completions"`,
    `banner=("claurst",)`, `composer=("❯",)` (U+276F, **not** ascii `>`), `dialogs=[("esc
    close","\x1b"),("keyboard shortcuts","\x1b")]`, `summ_markers=("concise yet thorough
    conversation summaries","conversation summar")`.
- **`settle_and_dismiss(driver, cfg, rounds=6)`** — the spike's fixed settle window: `rounds` ×
  (`pump(2.5)` → if any `cfg.dialogs` marker on screen, send its keys with 0.3s between chars →
  `pump(5, want=cfg.composer)`). Absorbs codex's immediate trust dialog **and** opencode's *late*
  update modal. **Never sends Enter while a dialog could be up.**
- **`submit(driver, cfg, wire, text, want=None, tries=3)`** — the wire-confirmed input primitive
  (spike `submit`): per try → `settle_and_dismiss` → record `before = wire.requests()` → `send(text)`,
  0.6s, `enter()` → `pump(30, want=want)` → **confirm `wire.requests() > before`**; retry if a
  racing overlay ate the Enter (mandatory for claurst onboarding). Returns bool landed. Text is
  always `?`-free (claurst opens help on `?` and eats later prompts).
- **`one_shot_inflate(engine, magnitude)`** — monkeypatch `ScriptEngine.next_reply` to
  `dataclasses.replace(r, prompt_tokens=magnitude)` **only on the first reply** (`_state["n"]==1`),
  else pass through. Returns a restore callable; the fixture installs/uninstalls it. One over-budget
  turn → exactly one compaction decision, never the unbounded runaway (opencode 1184 reqs/592 summ).

### 8.3 `fixtures.py` — reuse the gate fixtures, capture the real launch argv

- **Reused unchanged** from `tests/slow/gates_fixtures.py`: `PROXY_PORT` (11455), `MODEL_TAG`
  (`"stub"`), `scripted_backend(script, tmp)` (stub+proxy+tally+`capture_file`),
  `provisioned_sandbox(name, harness, tmp)` (generate proxy-dialing config, provision with
  `allow_hosts=("localhost:11455",)`, teardown). No forking.
- **`_CaptureLaunchRunner(Runner)`** — the spike's launch interceptor: overrides `run()` so a
  `why.startswith("launch")` call **captures** `(cmd, env)` and returns without exec'ing (every
  other `run` is real). This is what makes the driver launch danno's *real* `sbx exec -it …` argv.
- **`launch_argv(harness, cfg, backend) -> (exe, args, env)`** — calls the real
  `sandbox.launch(runner, name, target, harness=harness, capture_relay_port=PROXY_PORT,
  model=cfg.model)`, then `exe = shutil.which(cmd[0]) or cmd[0]`, `env = {**os.environ,
  **(runner.launch_env or {})}` with `TERM` forced off `"dumb"` → `"xterm-256color"`.
- **codex config graft** — codex needs a *small* top-level `model_auto_compact_token_limit` to
  compact under the inflated usage (spike monkeypatch 2): wrap `book_em_danno.config.generate.
  codex_config_toml` to prepend `model_auto_compact_token_limit = 200\n` **before** any
  `[model_providers.*]` header (top-level or codex ignores it). Applied only for the codex row.
- **`WireMetrics`** — a tiny reader over `backend.capture_file` unifying the two spike
  `wire_summary` variants behind one shape: `.requests()` (POST count to `cfg.wire_path`),
  `.summarization_requests()` (count of bodies whose `messages`/`input` JSON contains any
  `cfg.summ_markers`), `.item_counts()`. CHAT reads `body["messages"]`, RESPONSES reads
  `body["input"]` — keyed off `cfg.wire_path`, matching `gates_fixtures._inference_request_bodies`.

### 8.4 `test_tui_launch.py` — the parametrized A/H/C test

```python
pytestmark = [pytest.mark.slow, pytest.mark.sandbox]

@pytest.mark.skipif(sandbox_runtime_down(), reason="sandbox runtime down (sbx ls)")
@pytest.mark.parametrize("harness", ["opencode", "codex", "claurst"])
def test_interactive_launch(harness, tmp_path):
    cfg = HARNESS[harness]
    restore = one_shot_inflate(stub_script.ScriptEngine, cfg.inflate)
    try:
        with scripted_backend([Finish("Hello from the stub.")], tmp_path) as backend:
            with provisioned_sandbox(f"danno-tui-{harness}", harness, tmp_path) as target:
                exe, args, env = launch_argv(harness, cfg, backend, name=…, target=target)
                try:
                    driver = make_driver([exe, *args], env, prefer="auto")
                except DriverUnavailable as e:
                    pytest.skip(str(e))          # loud, named skip — never a silent pass
                driver.start()
                wire = WireMetrics(backend.capture_file, cfg)
                # A — reach TUI
                assert driver.pump(90, want=cfg.banner)
                settle_and_dismiss(driver, cfg)
                assert any(c in driver.screen().lower() for c in cfg.composer)
                assert "not a terminal" not in driver.screen().lower()
                # H — one turn on the wire
                assert submit(driver, cfg, wire, "list the files here", want="hello from the stub")
                assert wire.requests() >= 1
                # C — compaction, branched on capability (§5)
                for p in ("and again please", "one more time"):
                    submit(driver, cfg, wire, p)
                driver.pump(5)
                if get(harness).compacts:
                    assert wire.summarization_requests() >= 1     # opencode/codex
                else:
                    assert wire.summarization_requests() == 0     # claurst change-detector
                driver.close()
    finally:
        restore()
```

Notes: one sandbox provisioned per parametrization (module/function scope TBD by run time — the
spike provisions per harness); the `sandbox` marker lets `-m sandbox` select just these; the
`slow` marker keeps them out of the fast gate; `tests/conftest.py`'s autouse `delenv` already
keeps provision+exec on one sbx CLI for slow tests (§7).

---

## 9. Phased implementation

Phases are labelled with their owner — **[mac]** = this Claude (macOS), **[win]** = the Windows
Claude. The macOS lane runs first and freezes the shared contract; the Windows lane stacks on it.

1. **P0 [mac] — capability flag.** Add `Harness.compacts` (default True) + claurst `False`.
   Unit-assert registry values in the *fast* suite (no sandbox needed). Gate green.
2. **P1 [mac] — shared scaffolding + `PexpectDriver`.** Write the `TuiDriver` protocol,
   `primitives.py`, `fixtures.py`, the parametrized `test_tui_launch.py`, and `PexpectDriver`.
   Bring **all three** harnesses A/H/C green on macOS (this is the hardened spike code). Land a
   `WinPtyDriver` **stub** (`raise NotImplementedError`) so the Windows lane has a compile target,
   and the `.docs/results-slow-sandbox-tui/` template. **Exit gate = the `TuiDriver` protocol is
   frozen** (the handoff artifact, §10).
3. **P1-handoff [mac→win].** Push `slow-sandbox-tui-tests` green on macOS; record `macos.md`
   results. The Windows Claude branches from this tip.
4. **P1.5 [win] — manual danno-on-Windows smoke (blocking gate, no test code).** *After* the
   handoff, *before* any driver work, prove danno's runtime frame by hand on the Windows box:
   `docker sandbox`/`sbx` resolves; `danno sandbox start` (or a bare `sbx exec -it <name> echo ok`)
   actually launches an interactive `-it` exec; the `-e NAME` env-forward and `-w <target>` survive
   from **both** a cmd and a PowerShell window. This decouples **R1b** (does danno work here at all)
   from **R1a** (can pywinpty drive it). *Outcomes:* **pass →** proceed to P2. **fail
   (danno-product) →** fix danno's root cause on this platform **now** (in `sandbox_cli.py` etc.),
   land it on the branch stack, and re-run the smoke until it passes — *then* proceed to P2. We fix
   the platform here so the driver is later built against a **working** runtime; we do not defer the
   fix and stall the whole lane. Record the break **and its fix** in
   `windows-cmd.md`/`windows-powershell.md` and update `plan-test-danno-cross-platform.md` as the
   record. **fail (runtime absence) →** if Docker Desktop/sbx is genuinely unsupported on this build
   (not danno's code), that's the one honest loud-skip. Same smoke inside WSL2 gates P3 (§10.3).
5. **P2 [win] — `WinPtyDriver` + Windows validation.** Implement `WinPtyDriver` (pywinpty, §3.4)
   against the frozen protocol — **no changes to shared code**; if the protocol is wrong, file it
   back to [mac], don't fork. Validate all three harnesses A/H/C **from a cmd session and from a
   PowerShell session** (same driver, two shells). Pin the exact `pywinpty` version. Record
   `windows-cmd.md` + `windows-powershell.md`.
6. **P3 [win] — WSL2 validation.** Inside WSL2 on the same PC, run the suite **unchanged** (WSL2 is
   Linux → `PexpectDriver`), *gated by the WSL2 P1.5 smoke* (§10.3). Record `wsl2.md`. No new driver
   code — this is the POSIX path on Windows hardware.
7. **P4 [mac+win] — rollup + cross-platform report.** Merge the Windows branch into base;
   consolidate the results matrix; root-cause any degrade/break per
   `plan-test-danno-cross-platform.md` (H7 `host.docker.internal`, egress-allow, arg quoting).
   **No platform is claimed green until its results file exists from a real run.**
8. **P5 [mac] — docs + memory.** Update `research-tui-interactive-testing.md` §9, changelog, memory.

Each phase ends on `ninja check` green **on its platform**; nothing merges to main; nothing pushed
without the user's ask (Constitution v2.2.0).

---

## 10. Work division across Claudes & platforms

### 10.1 How many Claudes — the 2-vs-3 answer

**Two Claude Code instances, on two machines.** cmd and PowerShell do **not** need separate
Claudes, and WSL2 does not need a third author:

- **cmd vs PowerShell = one Claude, one driver, two runs.** `WinPtyDriver` is a single Python
  codebase; the pty-driving of `sbx exec -it` is byte-identical regardless of which shell launched
  it. cmd and PowerShell differ only in the **outer shell** you type the `pytest` command into —
  env-var syntax (`set X=…` vs `$env:X=…`), quoting, path handling. So the Windows Claude
  *implements the driver once* and *runs the suite twice* (from a cmd window, then a PowerShell
  window), recording each. Both runs matter (they exercise danno's own shell-quoting/env hazards,
  H1–H10), but they are **not** two implementations.
- **WSL2 = no new author, just a runner.** WSL2 is Linux, so it runs the **same `PexpectDriver`**
  the macOS Claude wrote — unchanged. It needs a runner *with WSL2 access*, not a new driver.
  Because WSL2 lives on the Windows PC, the Windows Claude opens a WSL2 shell and runs the POSIX
  suite there. That folds WSL2 into the Windows instance → still **two Claudes total**.

**Optional third Claude (`[wsl]`)** — only if you want WSL2 validated *in parallel* with the
cmd/PowerShell work, or in a separate checkout. Not required (WSL2 reuses POSIX code), so the
baseline is two; spin up a third only to parallelize.

| Claude | Machine | Validates | Driver(s) | Branch | Writes results to |
|---|---|---|---|---|---|
| **[mac]** (this one) | macOS | macOS | `PexpectDriver` + all shared code | `slow-sandbox-tui-tests` (base, off `main`) | `macos.md` |
| **[win]** | Windows PC | Windows-cmd, Windows-PowerShell, **WSL2** | `WinPtyDriver`; reuses `PexpectDriver` for WSL2 | `slow-sandbox-tui-tests-windows` (stacked on base) | `windows-cmd.md`, `windows-powershell.md`, `wsl2.md` |
| *(opt) [wsl]* | WSL2 on the PC | WSL2 only | reuses `PexpectDriver` | `slow-sandbox-tui-tests-wsl2` (stacked on base) | `wsl2.md` |

### 10.2 Contract-first handoff (why order matters)

The Windows lane is **blocked on a stable `TuiDriver` protocol** — it can't implement a backend
against a moving interface. So sequencing is strict:

1. **[mac] freezes the contract first** (P1 exit gate): the `TuiDriver` protocol, the
   `submit`/settle/inflate primitives, the per-harness marker table, the `fixtures.py` seams, and
   a `WinPtyDriver` **stub**. All pushed green on macOS.
2. **[win] stacks on that tip** and fills in `WinPtyDriver`. If [win] finds the protocol can't
   express something ConPTY needs, it opens an issue/PR back to [mac] to change the *shared*
   contract — it never forks the shared **test** files on the Windows branch. This keeps one source
   of truth for the test contract.
3. **danno-product fixes are welcome on the Windows branch — they're the job, not scope creep.**
   When P1.5/P2/P3 surface an R1b break, [win] fixes danno's own code (`sandbox_cli.py` etc.) as
   normal `fix(...)` commits on the stack so the platform goes green. That is *not* the same as
   forking the shared test contract (which stays [mac]-owned): fixing danno source is the whole
   point of "fix the platform as we find it." Keep such fixes in their own commits (Conventional
   Commits, referencing the platform) so the rollup can attribute them.
3. **Merge order:** base (`slow-sandbox-tui-tests`) → windows branch → main, each only when green
   and only on the user's explicit ask.

### 10.3 Preconditions each Claude confirms before starting

- **[mac]:** sbx up (`sbx ls`), `pexpect`/`pyte` importable, the three spike drivers still green.
- **[win]:** Docker Desktop + `sbx` (or `docker sandbox`) present on Windows; Python 3.13 + `uv`;
  `pywinpty` importable and ConPTY available (Windows 10 1809+); a **local** stub+proxy runs (no
  Mac needed, §7); **the P1.5 danno-on-Windows smoke passed** (§9). If sbx/`docker sandbox` isn't GA
  on that Windows build, the suite **skips loud** and the results file records "runtime
  unavailable" — that is a valid, honest outcome.
- **[win] → WSL2 (product surface, not just "Linux"):** before P3, confirm Docker Desktop's **WSL
  integration is enabled** for the distro; decide the checkout location (**native ext4 preferred**,
  `/mnt/c` is slow + has path-translation quirks — avoid for the real run); `docker`/`sbx` inside
  the distro reaches the same engine; and `host.docker.internal:<PROXY_PORT>` resolves **from a
  WSL2-backed container**. Run the **same P1.5 smoke inside WSL2** — a failure here is a
  **danno-product** WSL2 gap that we **fix in-lane** (root-cause in danno, land on the branch,
  re-run until green), not a driver bug and not a backlog item. Only the `PexpectDriver` is reused
  unchanged; the *runtime* is a fresh unknown, and getting it green is part of finishing the lane.

### 10.4 How results are recorded

Every run writes one markdown file under `.docs/results-slow-sandbox-tui/<platform>[-<shell>].md`
from a fixed template so the files are comparable and a rollup matrix can be regenerated:

```
# <platform> · <shell> — slow-sandbox-tui results
date: 2026-07-DD   (absolute; scripts can't call Date.now)
host OS / version · shell (cmd|powershell|bash) · Python x.y.z
sbx <ver> · pexpect <ver> | pywinpty <ver> · driver: PexpectDriver|WinPtyDriver|TmuxDriver(fallback)
fidelity: host-pty (real sbx exec -it)  |  tmux-fallback (record which and why)

| harness  | A | H | C | classification | leg | notes / root-cause |
|----------|---|---|---|----------------|-----|--------------------|
| opencode | ✅ | ✅ | ✅ | works          | —   | …                  |
| codex    | … | … | … | works/degrades/breaks | test-harness \| danno-product \| runtime | … |
| claurst  | … | … | 0 | works (compacts=False change-detector) | — | … |
```

- **Classification** uses the cross-platform plan's taxonomy: **works / degrades / breaks**, each
  break root-caused to a file:line so it becomes a fix we land in-lane, not a shrug or a deferral.
- **Leg attribution (required on every break)** — drives *who fixes it*, not whether: tag which leg
  owns it — **test-harness** (the driver/pty/`pyte` rig, e.g. pywinpty can't drive sbx → R1a),
  **danno-product** (danno's own Windows/WSL2 code path is wrong → R1b), or **runtime** (Docker
  Desktop / sbx / WSL integration absent). **test-harness** and **danno-product** breaks are both
  **fixed in-lane** — root-cause and land the fix (in the driver, or in danno's own code) on the
  branch stack until the test+platform combo is green; the results file records the break **and the
  fix that cleared it**, and `plan-test-danno-cross-platform.md` is the fix record. A **runtime
  absence** is the only break we can't fix in code → loud skip. *Never* patch the test to force
  green — fix the leg that's actually broken.
- `.docs/results-slow-sandbox-tui/README.md` holds the **rollup matrix** (one row per
  platform×shell) that each Claude appends its summary line to — the single at-a-glance status.
- Optionally emit `pytest --junitxml` alongside so the matrix is machine-regenerable; the markdown
  is the human record of record.
- **No silent green:** a platform's cell is filled only from an actual run on that platform; a
  skip (runtime down, pywinpty missing) is recorded as **skip + reason**, never as pass.

---

## 11. Risks & open questions

- **R1a — the test harness (host-pty) is unexercised on Windows (bounded, not fidelity).**
  `WinPtyDriver` is first-of-its-kind here. *Mitigation:* §3.4 verified the API maps cleanly and P2
  is a dedicated validation pass; the cost is a **version-pin + validation**, not a fidelity gap (on
  Windows the host-pty path is the faithful peer of POSIX, §3.2). *Fallback if ConPTY can't drive
  sbx on a given build:* `make_driver(prefer="tmux")` on that platform, recorded as reduced
  fidelity — never silently. This risk is about the **test rig**, not danno itself.
- **R1b — danno's *own* Windows / WSL2 code path is wholly unproven (product risk, distinct from
  R1a).** danno has never run on Windows-native or WSL2. Independent of whether pywinpty can drive a
  pty, the **product** may not work: `resolve_backend()` finding `docker sandbox` on that box; the
  `-e NAME` env-forward and `-w <target>` argv surviving cmd/PowerShell quoting + Windows path
  separators; `host.docker.internal` resolving from a WSL2-backed container; Docker Desktop's sandbox
  plugin even existing on Windows. *Mitigation:* the new **P1.5 smoke** (§9) proves the danno runtime
  frame by hand *before* any driver code, so an R1b break is caught cheap and attributed to the
  product, not chased through a new test rig. **A confirmed R1b break is fixed in-lane** — we
  root-cause it in danno's own code (`sandbox_cli.py` backend resolution, argv/quoting, path
  handling, hdi/egress) and land the fix on the same branch stack, so the test **and** the platform
  go green together. We do **not** push it to a backlog and move on: deferring the fix strands the
  test+platform combination in a permanently-red/skip state, which defeats the point of the pass.
  The cross-platform plan (`plan-test-danno-cross-platform.md`) is the **record of the fix**, not a
  parking lot. Never weaken the test to force green — fix the platform instead. *(The one thing we
  can't fix in-lane is a **runtime absence** — Docker Desktop / sbx genuinely unsupported on that
  build; that's not danno's code, so it stays a loud skip, R1a/runtime leg.)*
- **R2 — moving protocol breaks the handoff.** If [mac] keeps changing `TuiDriver` after [win]
  starts, the Windows branch churns. *Mitigation:* the P1 exit gate freezes the contract;
  post-freeze changes go through [mac] as shared-code PRs, not Windows-branch forks (§10.2).
- **R3 — opencode auto-update reaching the internet despite `localhost`-only egress** (seen in
  spike). *Flag:* if the updater bypasses egress it's a **sandbox-escape signal** — investigate
  separately; the test must ESC the modal fast and never let an update mutate the harness mid-run.
  Applies identically under pexpect and pywinpty.
- **R4 — Windows `host.docker.internal` resolution + egress.** The VM must reach the Windows host's
  local stub+proxy via `host.docker.internal:<PROXY_PORT>`, egress-allow that host:port only, never
  `**`. P4 nails the exact allow value on Windows; skip loud if resolution fails.
- **R5 — ConPTY byte-stream vs. pyte (§3.4).** ConPTY emits more repaint/reflow sequences than a
  Unix pty, so the *rendered* screen can differ under identical API calls. Match on stable markers
  after a settle, not transient paint; this is exactly what P2's validation pass shakes out.
- **R6 — tmux fallback fidelity.** If tmux is used, it does **not** run danno's real launch command
  (§3.2). Acceptable only as a recorded fallback with the reduced-fidelity note; a platform on the
  fallback is not counted as full host-pty coverage.
- **Q1** — Also gate a **local-Ollama-qwen** interactive smoke as a *separate* manual marker
  (`-m sandbox_live`)? Proposed: yes, documented, not in the default gate.
- **Q2** — Add GitHub Actions Windows/WSL2 rows? Only the *portable-Python* surface (Tier 2) — the
  sandbox tier stays hardware-in-the-loop (hosted runners have no sbx/Ollama).

## 12. Out of scope

- Claude Code interactive tests (standing exclusion).
- Real-model (non-stub) assertions in the gated suite — manual escalation only.
- Any egress weakening to make a platform pass — a fail-loud skip is correct; `**` is never
  acceptable (standing security invariant).
