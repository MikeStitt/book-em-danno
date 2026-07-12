# Plan — migrate danno from `docker sandbox` to `sbx` (dual-CLI during transition)

**Date:** 2026-07-09 · **Status:** **IMPLEMENTED (P1–P5) + W1, W2, W3, W5, W6, W7
DONE; W4 DEFERRED; spikes S1–S3 PASS (2026-07-11); gate green; all four harnesses
E2E-verified under `sbx` on macOS** (see
[`sbx-migration-e2e-validation-2026-07-11.md`](sbx-migration-e2e-validation-2026-07-11.md)).
Stacked PRs: #76 (base: W1/W2/S3-flip) ← #80 (W3) ← #81 (W5) ← #82 (W6) ← W7.
**W4 (relay-free occ) deferred** — unreliable multi-turn; occ keeps its relay.
Remaining follow-ups: S4 (user), `sbx secret` (D4), the configurable-claurst-timeout
build, the sweep `capture_port` wiring, and relay-free occ (undici fix). ·
**Branch base:** `main`.
**2026-07-10 update:** an independent review refuted the "sbx has no
`host.docker.internal` rewrite" premise (see
[`sbx-egress-model.md`](sbx-egress-model.md) §0) — the Phase-2 section below was
**rewritten** accordingly; passages marked *[REFUTED 2026-07-10]* are kept as the
historical record of what shipped.

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
  proxy-rewrite; sbx does not). *[REFUTED 2026-07-10: sbx has the same rewrite; the
  correct sbx token is `localhost:11434`, identical to docker — see the corrected
  Phase-2 below.]* `configure_proxy` derives it from the Ollama
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
route — `utun6`.)
*[REFUTED 2026-07-10: the mechanism is real, but the workaround is unnecessary AND
harmful — sbx rewrites `host.docker.internal`→`localhost` exactly as docker did, and
the `127.0.0.1:11434` token 403s that documented path (matching is literal on the
post-rewrite string). Workaround #2's removal trigger has FIRED; W1 below retires
it. The `#263` citation was a mis-cite (NVIDIA/OpenShell — a different project).]*

## Phase 2 — CORRECTED (2026-07-10; supersedes the withdrawn relay-based plan)

A second independent session re-verified the mechanism against the official docs, a
fresh live matrix, and **real harness turns** (full record:
[`sbx-egress-model.md`](sbx-egress-model.md) §0; legacy re-verification in W7 below).
The findings that reshape Phase 2:

- **sbx rewrites `host.docker.internal`→`localhost` before policy matching**
  (officially documented + live 200). The correct sbx egress config is therefore
  **identical to the docker one**: allow token `localhost:11434`, harness baseURL
  `http://host.docker.internal:11434/v1`. No resolver, no LAN IP, no loopback token.
- **opencode needs NOTHING**: a real `opencode run` turn (gemma4:26b) succeeded
  under sbx with the normal config, boundary 403s intact — Bun fetch honors the
  injected proxy env. sbx also injects `NODE_USE_ENV_PROXY=1` (Node fetch verified
  200) and **accepts CONNECT to an allowed non-443 port** (verified 200/403), so
  occ's undici path works on sbx too.
- **The relay works on sbx UNCHANGED** under the `localhost:11434` token (its exact
  `ProxyHandler`-opener pattern verified in-sandbox → 200): upstream stays
  `host.docker.internal`, urllib env-proxies it, no `no_proxy` surgery, no loop risk.
- **Legacy `docker sandbox` re-verified** with danno's exact flags: contract holds
  (internet 200 · LAN 403 · unallowed host ports blocked · Ollama hole 200) — but
  its denials are **HTTP 500 with a policy body**, not 403 (W7).
- **Loopback-only host services are reachable through BOTH proxies when allowed**
  (an allowed port reached a `127.0.0.1`-bound host server → 200): the
  `OLLAMA_HOST=0.0.0.0` prerequisite looks obsolete and is the less-safe option (S3).

### Work items (before un-drafting #76)

- **W1 — retire workaround #2 (the loopback resolver). ✅ DONE 2026-07-11.** On sbx,
  local Ollama aliases use the default `localhost:11434` token — the same
  `DEFAULT_ALLOW_HOSTS` path docker uses. Deleted `resolve_ollama_hostport`,
  `_LOCAL_OLLAMA_ALIASES`, `_SBX_LOOPBACK`, `[sandbox].resolve_ollama_host` (schema +
  tests); a concrete **remote** `host:port` stays a literal passthrough (now
  backend-agnostic, §7 of the egress doc). Scrubbed the refuted claims +
  `OpenShell#263` mis-cite from `sandbox.py`/`sandbox_cli.py` docstrings and retired
  the workarounds ledger (row 2).
- **W2 — per-harness verification gates on sbx. ✅ DONE 2026-07-11** — full record
  in [`sbx-migration-e2e-validation-2026-07-11.md`](sbx-migration-e2e-validation-2026-07-11.md).
  All four harnesses cleared L0+L1 through `danno validate` on `sbx v0.34.0` against
  host Ollama (`gpt-oss:20b`): **opencode** (relay-free) ✓, **claude** baseline
  (`api.anthropic.com` in `balanced`) ✓, **occ** (relay) ✓, **claurst** (relay) ✓.
  Boundary probe held: Ollama 200; `example.com`/LAN/gateway/other host port all 403.
  Two harness-leg issues surfaced (orthogonal to sbx): occ/claurst need the local
  backend **named `ollama`** (PR-#68 follow-up), and claurst's install raced the
  shell VM boot-apt lock (fixed on this branch). claurst+occ ran via the unchanged
  relay (W3/W4 relay-free remain follow-ups).
- **W3 — relay-free claurst on BOTH backends. ✅ DONE 2026-07-11 (PR #80).** danno
  sets `OLLAMA_HOST=http://host.docker.internal:11434` and drops the relay bracket
  from the claurst launchers (`driver.claurst_run`, interactive
  `interactive_launch_script`); the relay is retained only for `--capture` (W6). Works
  on legacy too. Live-verified: claurst L0+L1 pass on sbx and legacy.
- **W4 — relay-free occ on sbx ONLY. ⛔ DEFERRED 2026-07-11 — occ keeps the relay.**
  Spike S2 proved a *single* relay-free occ turn works, but implementing W4 and
  driving the *multi-turn* L1 loop exposed **intermittent unreliability**: the exact
  same command completes in 9–48 s manually (5/5 correct) yet the validator's capture
  errors at variable times (56 s, 183 s). The stalls sit in occ's undici↔proxy path
  (connection reuse / prefill variance), which the in-VM relay does not have. The
  relay path is fast and reliable (W2). Per fail-loud/no-regression, occ retains the
  relay on **both** backends; relay-free occ needs an undici connection-handling fix
  in the occ fork first (follow-up). Decision: user, 2026-07-11.
- **W5 — timeout parity on relay-free paths. ✅ VERIFIED 2026-07-11 (docs-only).**
  Relay-free claurst (W3) relies on the fork binary's **built-in 600 s** stall +
  reqwest timeouts, which comfortably cover the measured 98–157 s local prefills (W3
  L0/L1 ran in <20 s), so the lost `DANNO_RELAY_TIMEOUT` 3600 s headroom is not
  needed for realistic models. **Plan correction:** Bug 1 *hard-coded* 600 s in the
  pinned `v0.1.6-danno1` binary — it is NOT env-configurable (the earlier
  "fork Bug1 made it configurable" was wrong). Full env-tunable parity
  (`CLAURST_PROVIDER_STALL_TIMEOUT_SECS`, default 600) exists only as the **uncompiled**
  `fix/configurable-provider-stall-timeout` candidate; pinning that build is the
  follow-up. occ `CLAUDE_CODE_API_TIMEOUT` is moot — occ kept its relay (W4 deferred).
- **W6 — capture rewiring (claurst). ✅ DONE 2026-07-11.** claurst's `--capture` path
  now points `OLLAMA_HOST` directly at the host-side recording proxy
  (`host.docker.internal:<capture_port>`, opened in egress by `capture_allow_hosts`)
  instead of relaying to it — so claurst is **fully relay-free** (the relay bracket is
  gone from both claurst launchers; `_claurst_script` + the relay constants now serve
  only occ). Unit-verified; the relay-free mechanism is live-proven (W3, S1). occ's
  capture stays on the relay (W4 deferred). **Finding (pre-existing, orthogonal):** the
  validate *sweep* never threads `capture_port` to the claurst/occ `TurnFn`
  (`make_run_turn(env_file)` → `capture_port=None`), so claurst/occ `--capture` records
  nothing in the sweep — it routes to real Ollama regardless (true before W6 too; only
  the interactive `sandbox start --capture` path threads a port). Wiring per-model
  `capture_port` through the sweep is a **separate follow-up** (the port depends on the
  turn's backend, which the once-bound `make_run_turn` can't resolve). Buffered-streaming
  caveat unchanged.
- **W7 — backend-aware deny detection. ✅ DONE 2026-07-11 (no code — guidance
  captured).** sbx denies = **403**; legacy denies = **500** with body `connection to
  <host> blocked by network policy` (or a `dial tcp … connection refused` body when
  nothing listens — the legacy proxy connects-then-blocks: data never flows, but a
  listener's existence is detectable, a small port-scan side channel inherent to the
  deprecated proxy). Any automated boundary gate must judge **status + body per
  backend**, never exit codes or 403 alone. **danno has no automated boundary gate to
  make backend-aware** (the migration's boundary checks were done by hand — reading
  HTTP codes, e.g. the W2 probe), so this is a documented guardrail for any *future*
  gate, not a code change; adding an uncalled helper now would be speculative. The rule
  lives in three places: the README network-model section ("judge by HTTP status and
  body, never a tool's exit code"), this item, and the `sandbox-security-contract-
  fail-loud` memory (the verify-by-HTTP-403-not-exit-code lesson).
- **W8 — docs.** README network model split per backend (done on this branch);
  `sbx-egress-model.md` §0 corrections (done); ledger row 2 retired with W1;
  `--capture` README section updated with W6.

### Research spikes

- **S1 — claurst local-Ollama proxy honoring (gates W3). ✅ PASS 2026-07-11 (both
  backends).** Relay-free claurst (`OLLAMA_HOST=http://host.docker.internal:11434`,
  no relay) returned a real completion on **sbx** (gateway proxy) AND **legacy**
  (squid, absolute-URI plain-HTTP forward), exit 0. The fork build's reqwest DOES
  honor `HTTP_PROXY` on the Ollama path — the in-code "ignores HTTP(S)_PROXY" note is
  outdated. **No fork patch needed.** → W3 viable on both backends.
- **S2 — occ direct on sbx (gates W4). ✅ PASS 2026-07-11 (sbx).** Relay-free occ
  (`OPENAI_BASE_URL=http://host.docker.internal:11434/v1`) returned a real completion
  via the fork's undici `EnvHttpProxyAgent` (`[UNDICI-EHPA]` active), exit 0. → W4
  viable on sbx (relay stays for occ-on-legacy, as planned).
- **S3 — loopback-only Ollama end-to-end. ✅ PASS 2026-07-11 (both backends).**
  `validate --max-level 1` against an Ollama bound `127.0.0.1`-only cleared L0
  (reachability) on **sbx** (L0✓ L1✓) and **legacy** (L0✓ both runs; L1✓ on re-run,
  a small-model flake). **Flipped:** `doctor` now WARNs on a `0.0.0.0` bind and
  PASSes loopback-only (`ollama.lan_exposure_warning`); README prerequisite +
  command example recommend `OLLAMA_HOST=127.0.0.1:11434`. Both doctor branches
  verified live.
- **S4 — egress-posture decision (owner: user).** Legacy `--policy allow` = full
  public internet; sbx `balanced` = curated default-deny (`example.com` → 403 on
  sbx, 200 on legacy — both verified). The sandbox is a leg of the benchmarked
  triple, so the divergence is a **measured config dimension**: README now documents
  it; decide whether `danno.toml` grows a declared `[sandbox].extra_allow_hosts`
  (specific hosts only — the `"**"` prohibition stands) and whether bench
  `provenance.json` should record the backend + posture per run.

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
