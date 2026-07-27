# Windows-lane handoff — slow-sandbox-tui suite (P1.5 → P2 → P3)

**Date:** 2026-07-22 · **From:** the macOS Claude · **To:** the Windows Claude (on the Windows PC).

This is your concrete work order. The design of record is
[`plan-slow-sandbox-tui-tests.md`](plan-slow-sandbox-tui-tests.md) — read §3, §8, §9, §10, §11
in full; this file tells you **what to do**, in order, and pins the few facts that changed since
the plan was written.

---

## 0. What already exists (don't rebuild it)

The macOS lane (P0 + P1) is **done, green, and pushed** on branch `slow-sandbox-tui-tests`
(tip `68ba33b`, off `harness-api-add-codex`). On macOS all three harnesses (opencode, codex,
claurst) pass A/H/C — see [`results-slow-sandbox-tui/macos.md`](results-slow-sandbox-tui/macos.md).

The shared code you build on (**do not fork these — they are [mac]-owned and FROZEN**):

- `tests/slow/tui/driver.py` — the **`TuiDriver` protocol** (frozen), `PexpectDriver` (POSIX,
  works — you reuse it unchanged for WSL2), a `WinPtyDriver` **stub** (`raise
  NotImplementedError` — *this is the one file body you fill in*), and `make_driver`.
- `tests/slow/tui/primitives.py` — `HARNESS` marker table, `settle_and_dismiss`, `submit`,
  `one_shot_inflate`. Backend-agnostic (they call only the `TuiDriver` surface).
- `tests/slow/tui/fixtures.py` — `_CaptureLaunchRunner`, `launch_argv`, `codex_compact_graft`,
  `WireMetrics`.
- `tests/slow/tui/test_tui_launch.py` — the parametrized A/H/C test. You run it **unchanged**;
  it should pass verbatim once `WinPtyDriver` works.

**The rule:** if the `TuiDriver` protocol can't express something ConPTY needs, do **not** fork
the shared test files on the Windows branch — file the protocol change back to [mac] as a
shared-code PR (§10.2). You *do* freely fix **danno's own source** on your branch (that's the
job, see §fix-in-lane below).

---

## 1. Your branch

Branch **off `slow-sandbox-tui-tests`** (tip `68ba33b`), name it `slow-sandbox-tui-tests-windows`:

```
git fetch origin
git checkout slow-sandbox-tui-tests
git checkout -b slow-sandbox-tui-tests-windows
```

Never commit to `main`. Push only when `ninja check` is green **on Windows**, and only when
Mike asks. Never merge the PR yourself. Conventional Commits; end commit messages with
`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## 2. Preconditions to confirm before writing any driver code

Run these and record the results (they go into your results files):

1. **Docker Desktop + the sandbox plugin exist on Windows.** `sbx ls` (or `docker sandbox ls`)
   must resolve and list sandboxes. danno resolves the runtime via `sandbox_cli.base()` /
   `resolve_backend()`; confirm it finds one. If the sandbox plugin is genuinely **not GA** on
   this Windows build, that is the **one honest loud-skip** — record "runtime unavailable" in the
   results file and stop the Windows-native lane (WSL2 may still work — see §5).
2. **Python 3.13+ and `uv`.** `uv sync` installs the dev group; on Windows that pulls
   `pywinpty>=2.0` + `pyte>=0.8` (the `pexpect` line is POSIX-only via its marker, so it is
   correctly absent on Windows-native).
3. **`pywinpty` imports and ConPTY is available:** `uv run python -c "import winpty; import pyte"`.
   ConPTY needs Windows 10 1809+. If import fails, `make_driver` already raises
   `DriverUnavailable` → the test skips loud; fix the environment, don't weaken the test.
4. **A local stub+proxy runs on Windows.** The **gated suite is Ollama-free** (see §4): the stub
   AI + capture proxy are pure Python and run on the Windows box itself; the sandbox VM reaches
   them via `host.docker.internal:11455`. Nothing about the gated suite needs the Mac.

---

## 3. P1.5 — manual danno-on-Windows smoke (BLOCKING gate, no test code)

**Do this before any `WinPtyDriver` work.** It separates *"does danno's runtime frame work on
Windows at all"* (**R1b**, product) from *"can pywinpty drive it"* (**R1a**, test rig). By hand,
from **both a cmd window and a PowerShell window**:

- `sbx`/`docker sandbox` resolves;
- `danno sandbox start` (or a bare `sbx exec -it <name> echo ok`) actually launches an
  interactive `-it` exec;
- the `-e NAME` env-forward (issue #99 path) and `-w <target>` survive cmd/PowerShell quoting +
  Windows path separators.

**Outcomes:**
- **pass →** proceed to P2.
- **fail (danno-product / R1b) → FIX IT IN-LANE NOW.** Root-cause in danno's own code
  (`sandbox_cli.py` backend resolution, argv/quoting, path handling, hdi/egress), land it as a
  `fix(...)` commit on your branch, re-run the smoke until it passes, *then* proceed. Do **not**
  defer it to a backlog — that strands the test+platform combo permanently red. Record the break
  **and its fix** in `windows-cmd.md`/`windows-powershell.md` and update
  [`plan-test-danno-cross-platform.md`](plan-test-danno-cross-platform.md) as the fix record.
- **fail (runtime absence) →** Docker Desktop/sbx genuinely unsupported on this build → loud skip.

---

## 4. Ollama: the Mac serves it — but the GATED suite does not need it

**Mike is running Ollama on this Mac bound to `0.0.0.0` so the Windows PC can reach it over the
LAN.** Two clearly separate paths — do not conflate them:

### 4a. The gated stub suite (P2/P3 — what you're graded on): **no Ollama at all.**
`provisioned_sandbox` generates a config whose model backend dials the **local capture proxy**
(`http://host.docker.internal:11455/v1`), which forwards to the **local stub AI** — the "model"
is stubbed. The Mac's Ollama is irrelevant here. Run the suite with a stub+proxy on the Windows
box; nothing crosses the LAN. This is the coverage that counts.

### 4b. The OPTIONAL real-model escalation (plan Q1): point Windows danno at the Mac's Ollama.
Only if you want a real-model interactive smoke (not gated, documented-manual). The Mac exposes
Ollama on the LAN:

- **Mac LAN address:** `10.0.1.27` (hostname `peas.local`), port **11434**. Confirm reachability
  from Windows first: `curl http://10.0.1.27:11434/api/tags` (PowerShell:
  `curl.exe http://10.0.1.27:11434/api/tags`). If the IP has changed, re-check with `ipconfig
  getifaddr en0` on the Mac.
- **Point danno's Ollama backend at it.** danno defaults to `http://localhost:11434`
  (`ollama.DEFAULT_HOST_URL`). For the LAN path set the backend `base_url` in the danno config to
  `http://10.0.1.27:11434/v1` (or export the host override danno reads). The **sandbox VM** must
  then reach `10.0.1.27:11434`, so the **egress allow-list must name that host:port** —
  `provision(..., allow_hosts=("10.0.1.27:11434",))`. **Never `**`** — weakening sandbox egress is
  a blocking fail-loud defect (standing security invariant). host.docker.internal rewriting does
  **not** apply to a LAN IP; allow the literal `10.0.1.27:11434`.
- This path is where you'd also validate danno's real cloud/LAN egress behaviour on Windows if you
  want it, but it is **never** part of the green/red gate. Record it separately if you run it.

---

## 5. P2 — implement `WinPtyDriver`, validate Windows-native (cmd + PowerShell)

Fill in `WinPtyDriver` in `tests/slow/tui/driver.py` against the frozen protocol (plan §3.4 has
the verified pywinpty API map):

- `start()` → `winpty.PtyProcess.spawn([exe, *args], cwd=..., env=env, dimensions=(ROWS, COLS))`.
- `pump()` → non-blocking read via the §3.4 levers (`PYWINPTY_BLOCK`, `fileobj.settimeout`, or
  low-level `PTY.read(blocking=False)`), feed the decoded UTF-8 **`str`** into a `pyte.Screen`
  via a **`pyte.Stream`** (NOT `ByteStream` — pywinpty hands back str, not bytes).
- `send`/`enter` → `write("\x1b")` / `write("\r")`; `alive` → `isalive()`; `close` →
  `terminate(force=True)` then `close()`, all exception-swallowed (never raise in teardown).
- Match on **stable markers after a settle**, not transient paint — ConPTY emits more
  repaint/reflow than a Unix pty (§3.4 R5). The `HARNESS` markers already do this; if a marker
  needs a Windows-specific alternate, add it to the table as an *additional* any-of entry, don't
  replace the POSIX one.
- **Pin the exact `pywinpty` version** in `pyproject.toml` once it works, and record it.

Then run the suite **from a cmd session and from a PowerShell session** (same driver, two
shells — cmd/PowerShell differ only in outer env-var syntax + quoting, which is exactly danno's
H1–H10 hazard surface). Serial, not `-n` (the proxy/stub ports 11455/11456 are fixed):

```
uv run pytest tests/slow/tui/test_tui_launch.py -m sandbox -v -s -o addopts=""
```

Bring all three harnesses A/H/C green. Record `windows-cmd.md` + `windows-powershell.md` from the
template in [`results-slow-sandbox-tui/README.md`](results-slow-sandbox-tui/README.md), and append
your rollup rows there.

---

## 6. P3 — WSL2 validation (reuses `PexpectDriver`, no new driver code)

WSL2 is Linux → the **existing `PexpectDriver`** runs unchanged. But the *runtime* is a fresh
unknown, so before P3 confirm (plan §10.3):

- Docker Desktop's **WSL integration is enabled** for the distro; `docker`/`sbx` inside the distro
  reach the same engine.
- Checkout on **native ext4** (not `/mnt/c` — slow + path-translation quirks).
- `host.docker.internal:11455` resolves **from a WSL2-backed container**.
- Run the **same P1.5 smoke inside WSL2** first. A failure here is a **danno-product WSL2 gap** →
  **fix in-lane** (root-cause in danno, land on the branch, re-run until green), not a driver bug
  and not a backlog item.

Then run the suite unchanged and record `wsl2.md`.

---

## 7. Fix-in-lane policy (Mike's standing directive — do not violate)

> We fix the problems on the platforms as we find them. We don't weaken the test, but we do fix
> the root problems on the platforms as we find them. If we push the problem to the backlog, then
> we stall getting the combination of the test and the platform to work.

- **test-harness break** (pywinpty/pyte/driver rig, R1a) → fix in the driver.
- **danno-product break** (danno's own Windows/WSL2 code, R1b) → fix danno's source on your branch
  as `fix(...)` commits.
- **runtime absence** (Docker Desktop / sbx / WSL integration genuinely unavailable) → the only
  honest loud-skip; record "runtime unavailable".
- **Never** patch the test to force green; **never** weaken sandbox egress (no `**`) to make a
  platform pass. A fail-loud skip is correct; forcing a false green is not.

Every break in a results file is root-caused to a file:line with its leg attributed
(test-harness | danno-product | runtime), and the results file records the break **and the fix
that cleared it** (§10.4).

---

## 8. Definition of done for the Windows lane

- `WinPtyDriver` implemented; all three harnesses A/H/C green from **both** cmd and PowerShell.
- WSL2 green via the reused `PexpectDriver`.
- `pywinpty` pinned; `windows-cmd.md`, `windows-powershell.md`, `wsl2.md` written from real runs;
  rollup rows appended to the README.
- Any danno-product fixes landed as their own `fix(...)` commits, referencing the platform.
- `ninja check` green on Windows and in WSL2. Push when green; open the PR; **do not merge** —
  Mike merges (base `slow-sandbox-tui-tests` ← this windows branch ← `main`, §10.2).

Ping [mac] via a shared-code PR if the `TuiDriver` protocol needs to change — that's the only
thing that crosses back to the [mac]-owned files.
