# Plan — migrate danno from `docker sandbox` to `sbx` (dual-CLI during transition)

**Date:** 2026-07-09 · **Status:** **IMPLEMENTED (P1–P5), gate green, sbx default
live-verified on macOS** · **Branch base:** `main`.

## Implementation status (2026-07-09) — SHIPPED

All phases landed; `ninja check` green (571 passed); `danno doctor` live-shows
`PASS sandbox CLI (sbx)` on this Mac (both CLIs present).

- **P1/P2 — seam + selection.** New `book_em_danno/commands/sandbox_cli.py`:
  `resolve_backend()` (auto-prefer `sbx` via `shutil.which`; `DANNO_SANDBOX_CLI=
  sbx|docker` override; invalid value fails loud), `base()`, `label()`,
  `availability_argv()`, `policy_allow_argv()`. Every `["docker","sandbox",…]` call
  site in `sandbox.py`, `driver.py`, `suites/bench.py`, `run.py` now routes through
  `base()`. Verified live on macOS: no override → `sbx`.
- **P3 — semantic verbs.** `policy_allow_argv` maps the egress verb per backend,
  allowing **only the enumerated Ollama endpoint** — `sbx policy allow network
  --sandbox N <ollama-ip>:11434` on the `balanced` base (default-deny + curated
  dev/AI hosts) vs `docker sandbox network proxy N --policy allow --allow-host …`.
  **NEVER `"**"`.** The sbx allow must be the host's **routable LAN IP** (e.g.
  `10.0.1.9:11434`): `host.docker.internal` resolves to a link-local `fe80::1`
  inside the sandbox that the policy can't match (`docker sandbox` had a
  proxy-rewrite; sbx does not). `configure_proxy` derives it from the Ollama
  base_url and WARNS loudly if it's still a non-routable alias. Cloud auth stays on
  `--env-file` (works on `sbx exec` too — clean rename). **Verified end-to-end
  through `provision()` (2026-07-09):** `200` to the allowed Ollama, `403` to
  `example.com`, the LAN router, and even SSH on the Ollama host.
- **P4 — health checks.** `doctor` probes the active backend
  (`sandbox_cli.availability_argv()`); the portability probe already learned both.
- **P5 — tests + docs.** `tests/test_sandbox_cli.py` (selection + argv mapping);
  autouse conftest fixture pins `DANNO_SANDBOX_CLI=docker` so the 43 legacy argv
  assertions stay deterministic host-independently; this doc + module docstrings.

**Live end-to-end verification (2026-07-09, `sbx v0.34.0` on macOS)** — drove danno's
real `create → ensure_running → configure_proxy → exec → stop → rm` against the Mac's
Ollama (`192.168.1.5:11434`). Egress worked from inside the sbx sandbox via **both** the
LAN IP and `host.docker.internal`. The run surfaced **three bugs `docker sandbox` never
had, now fixed:**
- **`sbx create` requires a one-time global policy init** (`ERROR: global network policy
  has not been initialized`). Fix: `ensure_policy_initialized()` runs `sbx policy init
  balanced` before create when `sbx policy ls` shows it's absent (init is not idempotent).
- **`sbx rm` aborts on a non-tty** without `--force` ("stdin is not a terminal"). Fix:
  `rm_argv()` adds `--force` for sbx (docker takes no force flag).
- **`sbx ls` empty-state is prose, not a table** → the old header-skip parse returned a
  phantom `Launch`. Fix: `ls_names_argv()` uses `sbx ls -q` (bare names, empty when none).

**Resolved investigations (verified against `sbx v0.34.0`):** I1 exec = clean
rename (`--env-file`/`-i`/`-t`/`-w` all present) · I2 policy = `sbx policy allow
network [--sandbox N] RESOURCES`, `"**"` = allow-all · I3 create = agents
`shell`/`claude`/`opencode`/`codex` exist, no blueprint rename · I4 `sbx secret`
exists (proxy-injected service secrets) · I5 `docker sandbox` still present on
macOS Docker Desktop (dual-CLI window holds) · I6 `sbx version` is the probe.

**SECURITY MISTAKE ON FIRST SHIP — two errors, both corrected.** (1) The initial
migration mapped the egress verb to `sbx policy allow network --sandbox N "**"`
(allow ALL: internet + host + LAN + cloud metadata), **breaking danno's core
isolation contract**, pushed in PR #76 with a live agent turn under it, and filed as
a low-priority "deferred hardening" item — a **fail-loud violation / false success**.
(2) While fixing it I then wrongly concluded "sbx doesn't isolate at all" from a
**broken test**: `curl` without `-f` exits 0 on a 403, so I misread proxy-*denied*
responses as "REACHED." Reading actual HTTP codes showed sbx **does** enforce
(403 on every denied host). **Truth:** sbx enforces egress via a host HTTP(S) proxy
(`gateway.docker.internal:3128`); under `balanced` it is default-deny, and the fix
allows ONLY the Ollama endpoint by its **routable LAN IP** (`host.docker.internal`
is an unmatchable link-local `fe80::1`). Verified `200/403/403/403` through
`provision()`. PR #76 stayed a non-mergeable draft throughout. Lessons in the
`sandbox-security-contract-fail-loud` memory: weakening isolation is a blocking
fail-loud; and **verify the boundary by reading allow/deny signals (HTTP 403), not
proxy-tool exit codes.**

**Declared workarounds (config knobs, not silent hacks).** The two current sbx
accommodations are toggleable and grep-findable so they don't become OBE cruft — see
[`sbx-workarounds.md`](sbx-workarounds.md): `[sandbox].cli` (backend, SBX-TRANSITION)
and `[sandbox].resolve_ollama_host` (local-alias→IP resolution, SBX-WORKAROUND #263).
**Same-host reachability = loopback through the host proxy (verified).** An sbx
sandbox reaches a same-host Ollama at `127.0.0.1:port` **forced through the host-side
proxy** (the proxy's loopback is the host's): allowed→200, unallowed→403, other-port
→403. So `resolve_ollama_host` maps local aliases to `127.0.0.1` — network-independent
(no LAN IP, no VPN-interface guessing, works offline); a concrete/remote host stays
literal. (This replaced an earlier LAN-IP auto-detect that misfired on a VPN default
route — `utun6`.) **Phase-2, remaining before un-drafting #76:** the harness must route
`127.0.0.1` through the proxy — set `OLLAMA_BASE_URL=http://127.0.0.1:port` and drop
`127.0.0.1`/`localhost` from the harness `NO_PROXY` (keep `gateway.docker.internal`);
claurst keeps its own in-sandbox `127.0.0.1` relay. Verify a real turn per harness.

**Deferred (follow-ups, not blockers):**
- **D4 / `sbx secret`** — the migration keeps the working `--env-file` cloud-auth
  path (H4 unchanged). Adopting `sbx secret` (proxy-injected, never-exposed) is the
  recommended next step that also fixes the Windows H4 chmod-600 no-op.
- **Egress allowlist curation (NOT a security relaxation)** — the secure default is
  `balanced` + Ollama-host only. If danno's provisioning (git/apt/a custom pip index)
  needs a host `balanced` doesn't already permit, it will **fail loud** (egress
  denied); the fix is to add that *specific* host to the allowlist — never to widen
  to `"**"`. Curate the gap list from a live `danno install` run.

## Motivation

Docker deprecated the Docker-Desktop-integrated **`docker sandbox`** subcommand in favor of
a standalone **`sbx`** binary (`brew install docker/tap/sbx` / `winget install Docker.sbx`;
no Docker Desktop required). The 2026-07-09 Windows/WSL portability probe confirmed this
empirically: `docker sandbox --help` returned **exit 1** on cmd/PowerShell/WSL — the user
has switched those boxes to `sbx`. macOS Docker Desktop still ships the deprecated
`docker sandbox` for now, so danno must **support both CLIs during the transition**, then
default to `sbx`.

danno hardcodes `["docker", "sandbox", …]` in ~20 call sites; this plan introduces a single
seam and swaps the backend behind it.

## Research — `docker sandbox` vs `sbx` (the mapping)

`sbx` subcommands: `blueprint, create, exec, login, ls, policy, ports, reset, rm, run,
save, secret, stop, version`. Most map **verb-for-verb**; three surfaces changed shape.

| danno usage (`docker sandbox …`) | `sbx` form | kind |
|---|---|---|
| `create --name N IMG PATH` | `sbx create --name N IMG PATH` (e.g. `sbx create shell .`, `sbx create --name c claude .`; also `--memory 8g`) | ✅ clean rename |
| `ls` | `sbx ls` | ✅ clean |
| `stop N` | `sbx stop N` | ✅ clean |
| `rm N` | `sbx rm N` | ✅ clean |
| `version` / `--help` | `sbx version` / `sbx --help` | ✅ clean |
| `exec [--env-file F] [-it] N cmd…` | `sbx exec …` (⚠️ **flag parity unverified** — `--env-file`/`-it`; secrets may move to `sbx secret`) | ⚠️ **verify** |
| `network proxy N --policy allow` | **`sbx policy allow network <host>`** (per-host) / named policies Open·Balanced·Locked-Down; `sbx policy ls` | 🔴 **semantic change** |
| (n/a) | `sbx ports N --publish …` (new — port publishing now exists) | ℹ️ new capability |

Notes: agent images `shell` and `claude` **persist** under `sbx create` (good — danno's
`shell`/`claude` image names likely need no change; confirm `opencode`/`codex`). Docker
labels the feature **experimental** and warns "the API will change" — the seam must stay
thin and defensive.

## Where danno touches the sandbox CLI (call-site inventory)

All in `src/`. Grouped by verb:

- **create:** `commands/sandbox.py:319`
- **network policy:** `commands/sandbox.py:342` (`network proxy … --policy allow`) — 🔴
- **exec** (many): `driver.py:290, 463, 705, 911, 1102`; `commands/sandbox.py:361, 921,
  1210, 1344, 1352` — incl. `--env-file`, `-it`, and `exec N claude update` /
  `exec N opencode upgrade`
- **ls:** `commands/sandbox.py:114`
- **stop:** `commands/sandbox.py:352`, `suites/bench.py:139`
- **rm:** `commands/sandbox.py:1317`, `suites/bench.py:140`, `danno_validator/run.py:267`
- **availability check:** `commands/doctor.py:82` (`docker sandbox --help`) — and the probe's
  preflight (`scripts/portability/probe.py`, `docker sandbox --help`) — both must learn `sbx`.

## Design — one seam, two backends

Introduce `src/book_em_danno/commands/sandbox_cli.py` (name TBD) that owns *how the argv is
built*, so no other module hardcodes `["docker", "sandbox"]`:

- **`base_argv() -> list[str]`** → `["sbx"]` or `["docker", "sandbox"]` from selection (below).
- **Verb builders** for the surfaces that differ per backend: `policy_allow(name, host)`,
  `exec_argv(name, cmd, *, env_file=None, interactive=False)`, so the semantic differences
  live in ONE place, not sprinkled across `driver.py`.
- Pure-rename verbs (`create/ls/stop/rm/version`) just prepend `base_argv()`.

**Backend selection** (fail loud if neither present, Working Rule 8):
1. explicit override — env `DANNO_SANDBOX_CLI=sbx|docker` (and/or a `danno.toml` key);
2. else auto-detect — `shutil.which("sbx")` present → `sbx`; else `docker` with a working
   `sandbox` subcommand → `docker sandbox`;
3. else fail with the install hint for the platform.

Default policy when BOTH are present is a **decision point** (D1).

## The three non-trivial mappings (design + investigation)

1. **Network egress policy (🔴 the big one).** Today: `docker sandbox network proxy N
   --policy allow` sets a blanket allow, and danno's egress model relies on the proxy
   rewriting `host.docker.internal`→`localhost:11434`. `sbx` uses a **per-host allow**
   (`sbx policy allow network <host>`) with named base policies. Must decide how danno
   reproduces its egress posture — likely `sbx policy allow network <ollama-host>` (which
   dovetails with the remote-Ollama repoint: allow the **Mac's LAN IP:11434** explicitly)
   rather than a blanket allow. **Verify the allow-all / "Open" form and the exact per-host
   syntax against real `sbx`.**
2. **exec env/secret injection (⚠️ highest security risk).** danno injects cloud auth via a
   **chmod-600 `--env-file`** on `docker sandbox exec` (`driver.py:463/705/911`). Confirm
   `sbx exec` supports `--env-file` and `-it`; if secrets moved to **`sbx secret`**, danno's
   whole cloud-auth path (and the H4 chmod-600 discipline) must be re-mapped. **Verify
   `sbx exec --help`.**
3. **Image/blueprint names.** Confirm `shell`, `claude`, `opencode`, `codex` resolve under
   `sbx create` (shell/claude confirmed; the rest unverified). If sbx renamed any to
   "blueprints", update `_docker_image()` (`commands/sandbox.py:80`).

## Phased plan

- **P1 — introduce the seam, no behavior change.** Route every call site through
  `sandbox_cli` while it still emits `["docker","sandbox",…]`. `ninja check` green; a Mac
  `sbx`-free run behaves identically. Pure refactor.
- **P2 — add the `sbx` backend + selection.** Implement `base_argv()` selection and the
  clean-rename verbs. Live-verify each verb against **real `sbx`** (the Windows/WSL boxes)
  AND **real `docker sandbox`** (macOS, while it lasts).
- **P3 — migrate the semantic verbs.** `policy_allow` + `exec_argv` per-backend branches;
  live-verify egress (Ollama reachable from inside an `sbx` sandbox) and cloud-auth injection.
- **P4 — teach the health checks both CLIs.** `doctor` (`doctor.py:82`) and the probe
  preflight try `sbx version` then `docker sandbox --help`; report which backend is active.
- **P5 — docs + knob + deprecation note.** README / SAMPLE, the `DANNO_SANDBOX_CLI` knob,
  and a constitution `parts/` note if the sandbox contract is documented there. State the
  sunset intent for `docker sandbox`.

## Decision points (owner: user)

- **D1 — default backend when both are installed: DECIDED (2026-07-09) → default to
  `sbx`.** Selection auto-prefers `sbx` when present (`shutil.which("sbx")`), with
  `DANNO_SANDBOX_CLI=docker` as the escape hatch to force the legacy `docker sandbox`.
- **D2 — selection mechanism:** auto-detect only, explicit config only, or both (recommend
  both: detect, override wins).
- **D3 — how long to keep `docker sandbox`:** until macOS Docker Desktop removes it, or drop
  sooner once all dev machines are on `sbx`?
- **D4 — secret model:** if `sbx` prefers `sbx secret` over `--env-file`, adopt it (better
  than H4's ineffective-on-Windows chmod-600) or keep `--env-file` for parity?

## Investigations (verify against real `sbx` — the Windows/WSL boxes have it)

- **I1** `sbx exec --help` — `--env-file`, `-it`/interactive, working-dir/`-w` equivalents.
- **I2** `sbx policy --help` — exact per-host allow syntax + the allow-all/"Open" form; how
  to allow the Mac's `MAC_IP:11434` for remote Ollama.
- **I3** `sbx create --help` — available agent images/blueprints (shell/claude/opencode/codex),
  workspace/path + mount semantics, `--name`, `--memory`.
- **I4** `sbx secret` — the credential-injection model (affects cloud auth + H4).
- **I5** Does `docker sandbox` still work on the current macOS Docker Desktop (the dual-present
  window we depend on for P2)?
- **I6** `sbx version` output shape (for the doctor/probe availability check).

## Risks

- **R1 — experimental API churn.** Docker warns "the API will change." Keep the seam thin and
  pin behaviors behind it so a flag rename is a one-file edit.
- **R2 — secret model change** could alter the cloud-auth path and its (already Windows-broken,
  H4) chmod-600 discipline — treat as a chance to fix, not just port.
- **R3 — egress model change** (per-host `policy` vs blanket proxy) could break the
  `localhost:11434` / `host.docker.internal` assumptions the Ollama routing depends on.
- **R4 — image/blueprint renames** would silently fail `create`; verify before P2.

## Sources (web-verified 2026-07-09)

- [`sbx` CLI reference — Docker Docs](https://docs.docker.com/reference/cli/sbx/) ·
  [Get started with Docker Sandboxes](https://docs.docker.com/ai/sandboxes/get-started/) ·
  [`docker sandbox` (deprecated)](https://docs.docker.com/reference/cli/docker/sandbox/)
- [docker/sbx-releases](https://github.com/docker/sbx-releases) ·
  [Ajeet Raina — Run agents in microVMs with Docker sbx](https://www.ajeetraina.com/stop-running-agents-in-containers-run-them-in-microvms-with-docker-sbx/)
