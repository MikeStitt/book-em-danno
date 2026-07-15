# Plan — Long-lived validation scripts for the runaway gates (PR #88)

**Status:** DESIGN (2026-07-14, not yet implemented)
**Companions:** [`plan-bench-runaway-gates.md`](plan-bench-runaway-gates.md) (the
feature under validation — PR #88), [`plan-stub-ai-test-harness.md`](plan-stub-ai-test-harness.md)
(the deterministic stub AI this plan instantiates),
[`live-verify-runaway-gates.md`](live-verify-runaway-gates.md) (the manual live recipe
this plan automates).

## 1. Why long-lived scripts, not a one-off verification

PR #88's coverage today is: unit tests for the pure logic (gate config resolution,
`GateTally`, the watchdog against `sleep` subprocesses) plus **one manual live run**
(2026-07-14) that exercised **opencode × Gate 3 only**. Gate 1's graceful-first
option B, the claurst/occ paths, and every failure dialect were *not* live-verified.

The gates sit on ground that **drifts under us**, so validation must be re-runnable,
not archival:

- the sandbox template is unpinned — an opencode version bump can flip the V1→V2
  session runner and change `agent.steps` semantics (stub-AI plan §8.1);
- claurst is our own fork — every rebuild can change `--max-turns` and stream-usage
  behavior;
- the gate *sensor* is the capture proxy's usage extraction — any backend/dialect
  change (Ollama native vs `/v1`, `stream_options.include_usage`, Responses API)
  silently changes what Gate 1/2 can see.

A "runaway protection" feature whose own tests can hang or rot is self-defeating:
**every script below carries a hard `pytest-timeout` ceiling** — a regression fails,
never hangs (stub-AI plan §3.4, same rule).

## 2. Seed backlog — findings from the 2026-07-14 review of PR #88

The review (Claude, 2026-07-14, 8-angle pass over `main...docs-bench-runaway-gates`)
produced these findings; each maps to a validation script (§4) that reproduces it
deterministically and stays as its regression net after the fix.

| # | Finding (severity order) | Status at review | Validated by |
|---|---|---|---|
| F1 | Gate 1/2 tick **only on usage-bearing responses** (`proxy.py:160`), so any inference dialect without a `usage` block — Ollama-native `/api/chat` (`eval_count`, no `usage`), OpenAI-compat streams without `stream_options.include_usage` (claurst's known gap) — never advances the tally: Gate 1/2 **silently inert for claurst-local cells**, contradicting DoR §3.3's "✅ backstop at +grace" | confirmed (code-level) | V1 |
| F2 | `_capture_watched` kills only the direct child (`exec.py:297`, no process group / `start_new_session`); a host-side grandchild inheriting the pipes keeps `reader.join()` (no timeout) blocked → the anti-hang watchdog can itself hang after "killing" the cell | plausible | V2 |
| F3 | Reader-thread exceptions are swallowed (`exec.py:283`): a harness emitting invalid UTF-8 dies as a silent `""` stdout (`out[0] if out else ""`), diverging from the unwatched path which raises loudly — a fail-loud violation | plausible | V2 |
| F4 | `--no-save-captures` temp dir (`bench.py:276`) is removed only on the success path (`bench.py:766`, not a `finally`): an aborted run leaks full wire captures (prompts included) in `/tmp`; `--capture-dir` combined with `--no-save-captures` is silently ignored | confirmed | V4 |
| F5 | Provenance records the raw `GatesConfig` once (`provenance.py:170`), not the **per-cell resolved** values DoR §6 promised; harness *version* recording (stub-AI plan §8) also still absent | confirmed (drift) | V5 |
| F6 | README §"Capturing model wire traffic" still documents bench-era `--capture` semantics (e.g. `README.md:321`); `--no-save-captures` appears nowhere — constitution Documentation Hygiene | confirmed | (docs fix, no script) |
| F7 | `_REAP_PATTERN` pkill (`base.py:115`) self-matches its own `bash -lc` wrapper's cmdline → the reaper SIGKILLs its own `sbx exec` session (works only because `check=False`); noisy 137s and a latent footgun | confirmed (benign today) | V3 |
| F8 | `HARNESS_NAMES` (`suites/config.py:23`) is defined and never used — dead code | confirmed | (delete, no script) |

F1 is the load-bearing one: **the uniform Gate-1 backstop is the PR's core claim**,
and it currently depends on an accident of dialect. The candidate fix (count rounds
by request *path* — POST to `/v1/chat/completions` | `/v1/responses` | `/v1/messages` |
`/api/chat` — and use `usage` only for Gate 2's tokens) belongs in a follow-up on the
PR #88 stack; V1 is written red-first against it.

## 3. Architecture — three tiers, one stub

The stub AI (stub-AI plan §3) is the substrate: a scripted, deterministic model
backend behind the real capture proxy, so gate behavior is reproducible without a
live model. This plan implements the **subset of the stub plan the gates need**
(server + script engine + Tier B fixtures = stub plan M0/M1), sequenced by what
validates PR #88 — it is the stub plan's first consumer, not a fork of it.

| Tier | What runs | Docker? | Cadence |
|---|---|---|---|
| **0** (exists) | PR #88's unit tests (`test_capture_gate`, `test_exec` watchdog, `test_validator_suites` gates) | no | `ninja check`, every commit |
| **A** | stub AI ↔ capture proxy ↔ `GateTally`/watchdog, in-process; the dialect matrix (V1) and watchdog robustness (V2) | no | `ninja check`, every commit |
| **B** | real harness in the real sandbox against the stub AI on the host; termination matrix (V3), lifecycle checks (V4), drift detectors (V5) | yes | `-m slow`, on demand + before any bench campaign / after any template or fork bump |

Tier B honors the security invariant (memory `opencode-only-in-docker-sandbox` /
stub plan §3.4): a `tool_loop` script makes the harness **execute real tool calls**,
so the harness-under-test always runs inside the sandbox; only the stub lives on the
host, wired exactly where host Ollama sits today (`--allow-host` + capture proxy —
no new plumbing).

## 4. The scripts

Named V1–V5; each is a pytest module, parametrized where noted, with a hard timeout.

### V1 — Gate-sensor dialect matrix (Tier A) — *the F1 net*

`tests/test_gate_sensor_dialects.py`. Drive the **real capture proxy** (with a
`GateTally`) against a stub upstream that answers one inference request in each wire
dialect the harnesses actually produce:

| dialect row | example producer | must tick Gate 1? | carries tokens (Gate 2)? |
|---|---|---|---|
| chat-completions, non-stream, `usage` | occ via relay | yes | yes |
| chat-completions SSE **with** `include_usage` | opencode ↔ Ollama `/v1` | yes | yes |
| chat-completions SSE **without** usage | claurst pre-fix stream | **yes (red today)** | no (tokens 0 is fine) |
| Ollama native `/api/chat`, non-stream + NDJSON stream | claurst local | **yes (red today)** | no |
| Responses API SSE (`response.completed`) | opencode ↔ NVIDIA | yes | yes |
| discovery (`/api/tags`, `/v1/models`, `GET`s) | all, incl. title-gen | **no** | no |

Assertion shape: `tally.inference_calls()` after each row, plus the invariant
`rounds ≥ wire_metrics.request_count` (the sensor may never see *fewer* rounds than
post-hoc parsing). The two red rows encode F1 and go green with the path-based
round counter; until then they are the standing red test (TDD sequencing, stub plan §4).

### V2 — Watchdog robustness under hostile children (Tier A) — *the F2/F3 net*

`tests/test_exec_watchdog_hostile.py`, all real subprocesses, no Docker:

- **grandchild-holds-pipe:** spawn `python -c` that forks a child sharing stdout and
  then loops forever; breach Gate 3 → `runner.capture` must **return** within a
  bounded time (this is F2's red row; green = process-group kill and/or bounded
  `reader.join(timeout)` + loud warn).
- **invalid-UTF-8 emitter:** child writes bytes undecodable as UTF-8 then exits →
  watched capture must not silently return `""` (F3 red row; green = `errors=
  "replace"` or surfaced error — decide at fix time, the test asserts "not silent").
- **kill-latency bound:** a breach is enacted within `2 × _WATCH_INTERVAL_S + ε`.
- **no-breach parity:** a well-behaved child under the watchdog returns the same
  `CaptureResult` as the unwatched path (stdout/stderr/returncode equality), so the
  watched seam can never drift into a second behavior.

### V3 — In-sandbox termination matrix (Tier B) — *automates the live-verify doc*

`tests/slow/test_gates_termination_matrix.py`, parametrized
`harness ∈ {opencode, occ, claurst} × script`:

| stub script | asserted outcome |
|---|---|
| `finish` after 3 × `tool_call` | exits 0, no breach, transcript shows exactly 4 rounds, normal verdict |
| `tool_loop(forever)` | **the D1 row.** occ/claurst: graceful self-stop at `max_turns`, `breach is None`, complete transcript (option B's "graceful stop wins the race" — never live-verified in PR #88); opencode: external kill at `max_turns + grace`, verdict `runaway` |
| `tool_loop(forever)` + tiny `max_tokens` | verdict `over-budget` (Gate 2 has no live verification at all today) |
| `drip` slower than everything | verdict `timeout` (Gate 3 — re-pins the one row the manual run covered) |

Post-kill invariants, every row: `sbx exec <name> ps` shows **no surviving harness
process** (the reap, F7's noisy self-kill also observed here); the *next* cell in the
same sandbox runs clean (no bleed-through); `bench.json` row carries the breach in
`error` and the partial transcript sidecar exists.

### V4 — Config-is-code lifecycle checks (Tier B, cheap rows Tier A) — *the F4 net*

`tests/slow/test_gates_lifecycle.py` (+ fast rows in `tests/test_bench_capture_lifecycle.py`):

- `--no-save-captures`: after a **completed** run, no `danno-bench-cap-*` residue;
  after an **aborted** run (SIGINT injected mid-cell), still no residue (F4 red row).
- `--no-save-captures --capture-dir X`: fails loud or warns — never silently ignores
  `X` (F4 second half; pick the behavior at fix time, assert "not silent").
- malformed `[gates]` TOML (unknown key, wrong type, unknown harness name) → loud
  load-time rejection (extends the existing M0 tests to the file-through-CLI path).
- deprecated `--capture` warns and is otherwise a no-op.

### V5 — Drift detectors (Tier B, opt-in) — *the F5 net and the version canary*

`tests/slow/test_gates_drift.py`:

- **opencode `steps` semantics:** seed `steps=N`, run a `tool_loop` stub cell, assert
  on **wire round-trips in the stub transcript, not exit semantics** (stub plan §8.2 —
  at-cap behavior differs across V1/V2). If the count stops obeying `steps ?? ∞`
  rules, the sandbox template flipped runners — the test *names* that in its failure
  message.
- **provenance completeness:** `provenance.json` records the gates block **and** the
  harness version (F5); once per-cell resolved gates land, assert those too.
- **stub-vs-live framing diff** (opt-in, needs local Ollama): replay one live
  `/v1` + `/api/chat` exchange, diff SSE/NDJSON framing against the stub's — the
  stub-fidelity guard (stub plan A2).

## 5. Where the code lives (constitution fit)

These are **long-lived, first-class artifacts** — full standards, never `scratch/`
(the scratch escape hatch is for throwaway probes; these are the opposite):

- `src/book_em_danno/stubai/{server,script}.py` — the stub server + script engine
  (stub plan §3.1/§3.2 verbatim; precedent for test infra in `src/`:
  `capture/proxy.py`). Dialect framing seeded from existing `--capture` goldens.
- `tests/test_gate_sensor_dialects.py`, `tests/test_exec_watchdog_hostile.py`,
  `tests/test_stubai_server.py` — Tier A, in the `ninja check` gate.
- `tests/slow/test_gates_{termination_matrix,lifecycle,drift}.py` — Tier B, `-m slow`
  (Docker Desktop required, so on-demand on the dev machine, not CI).
- New dev dependency: `pytest-timeout` (uv-locked); every Tier B test sets a ceiling.

Red rows (F1–F4) are marked `xfail(strict=True)` until their fixes land, so the gate
stays green while the backlog stays loud — a silently-passing xfail fails the suite.

## 6. Milestones

- **GV0** — stub server + script engine (minimum: `finish`/`tool_call`/`tool_loop`/
  `drip` + the §V1 dialect table) + V1 + V2. Gate green with F1/F2/F3 as strict
  xfails. No Docker. *(≈ stub plan M0, scoped to gates.)*
- **GV1** — fix F1 (path-based round counting) + F2/F3/F4 on a branch stacked on
  PR #88; flip the xfails green. F6/F8 ride along (docs + dead code).
- **GV2** — Tier B fixtures (`stub_backend`, `harness_cell` — stub plan §3.4) + V3
  for opencode + occ. *(≈ stub plan M1 + the M2 red-row-goes-green moment.)*
- **GV3** — claurst row + V4 + V5; record the run-before-every-campaign rule in the
  bench docs; retire `live-verify-runaway-gates.md` §1–2 to "see the automated
  matrix" (keep §3's manual reap recipe).

## 7. Open questions

- **Q1 — F1 fix location:** path-based round counting is a behavior change to the
  gate sensor; land it as its own PR stacked on #88 (recommended — keeps #88's
  review surface stable) or amend #88? User's call at GV1.
- **Q2 — cadence enforcement:** Tier B can't run in CI (needs Docker Desktop + host
  Ollama for V5's live diff). Proposal: a documented pre-campaign checklist step in
  the bench README, plus running it after any sandbox-template/fork bump. Automation
  beyond that (launchd/cron on the dev Mac) is out of scope unless asked.
- **Q3 — `pytest-timeout` vs hand-rolled deadlines:** dependency preferred (battle-
  tested thread-based kill); confirm it plays well with the sandbox-exec subprocesses
  at GV2.
