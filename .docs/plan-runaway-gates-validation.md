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
and it currently depends on an accident of dialect. The subsections below give, per
finding, *why it is a problem* (which invariant it breaks and what goes wrong in the
field) and *the fix design* (mechanism, location, edge cases). Every fix lands on a
follow-up branch stacked on PR #88, red-test-first against its V# script.

### 2.1 F1 — the gate sensor is usage-blind (Gate 1/2 inert for claurst-local)

**Why it is a problem.** Gate 1 is the *primary* runaway detector — the whole DoR
reframe (§2 there) rests on round count being the speed-invariant unit, and §3.2
claims uniformity: *"Gate 1 is uniform across harnesses because the always-on proxy
counts rounds for all of them."* But the sensor's counting condition is not "an
inference round happened"; it is "`extract_usage` returned a value"
(`capture/proxy.py:160`). Those coincide only for dialects that carry a `usage`
block. claurst-local traffic does not: Ollama-native `/api/chat` reports
`eval_count`/`prompt_eval_count` (no `usage` key), its streaming form is NDJSON
(no `data:` prefix, so the SSE scanner in `extract_usage` skips every line), and
claurst's stream path is the known `include_usage_in_stream` gap. Consequences:

- **Defense-in-depth collapses to one layer.** For claurst-local the "external
  backstop at `+grace`" (DoR §3.3 table: ✅) can never fire; the only round bound is
  claurst's *own* `--max-turns` — in a fork we rebuild regularly, i.e. exactly the
  component whose regression the external gate exists to survive. If that cap
  regresses, the cell burns GPU for the full 1800 s Gate 3.
- **It fails silent, which is worse than failing.** Nothing warns that a cell's
  Gate 1/2 were inert; `provenance.json` records `max_turns`/`max_tokens` as if in
  force, so a reader gains *false* confidence (Working Rule 8 violation).
  `wire.request_count` also reads 0 for these cells, so the report under-states
  what the harness did.
- **The blindness is load-order fragile.** Whether claurst usage flows depends on
  which fork build is installed (the `fix/ollama-nvidia-stream-usage` branch adds
  stream usage on some paths) — the sensor's coverage changes with a binary swap
  that nothing records.

**Fix design.** Separate the two questions the sensor currently conflates:

1. *"Is this an inference round?"* → decide by **request path + method**, not by
   response contents: POST to `/v1/chat/completions` | `/v1/responses` |
   `/v1/messages` | `/api/chat` (+ `/api/generate` for completeness) ticks
   `tally.record(...)`. Discovery traffic (`GET`s, `/api/tags`, `/v1/models`,
   `/api/show`) stays excluded by the same rule. Title-gen POSTs would count, but
   bench already seeds `disable_title=True`, so scripted counts stay exact.
2. *"How many tokens?"* → `usage` when present, as today; **also** map
   Ollama-native `prompt_eval_count`/`eval_count` in `capture/usage.py` (a cheap
   win that makes Gate 2 and the token telemetry non-zero for local cells).

Alignment duty: `GateTally.record`'s docstring pins the tally to
`wire_metrics.parse_capture_records` — change both sides in the same commit (the
request *metric* row can keep `usage=None` fields; it must simply exist), or the
"rounds ≥ request_count" invariant becomes unverifiable. Fail-loud backstop
regardless of the fix: at cell end, if the proxy saw ≥1 POST but
`tally.inference_calls() == 0`, log a loud "gate sensor blind for this cell"
warning — that catches the *next* unknown dialect too.

**Proven by:** V1's dialect matrix — the two red rows (Ollama-native, usage-less
SSE) plus the `rounds ≥ request_count` invariant.

### 2.2 F2 — the kill doesn't cover the process tree; the watchdog can hang itself

**Why it is a problem.** The watchdog's entire value is a *termination guarantee* —
D1 was "`danno bench` blocks forever at the exec seam." `_capture_watched` kills
only the immediate child (`proc.kill()`, `core/exec.py:297`; no
`start_new_session`), then blocks on `reader.join()` with **no timeout**
(`exec.py:305`). A pipe reaches EOF only when *every* write-end FD closes; any
host-side helper the `sbx`/`docker` CLI forked (credential helpers, wrappers)
inherits those FDs. Kill the CLI and the helper keeps the pipe open →
`proc.stdout.read()` never returns → `join()` blocks → the run hangs *after* the
gate "fired," with the breach recorded but never reported. That is D1 reintroduced
one layer up, and it is strictly worse than the pre-PR hang because the operator
believes the gates make it impossible; there is no outer timeout above this seam by
design.

**Fix design.** Three cheap layers, all in `_capture_watched`:

1. Spawn with `start_new_session=True`; on breach, `os.killpg(proc.pid, SIGKILL)`
   (guarded for `ProcessLookupError`) so the whole host-side tree dies, then the
   existing `proc.wait()` + `on_kill` reap.
2. `reader.join(timeout=…)` (a few seconds); on expiry, log a loud warning and
   abandon the threads (make them daemons at creation so they cannot pin exit).
3. Note the abandonment in the breach rationale so a truncated transcript is
   *labeled* truncated, never silently short.

The in-VM reaper (`on_kill`) is unaffected — it solves the other half (the VM side)
and stays as is.

**Proven by:** V2's grandchild-holds-pipe row (bounded return time under
`pytest-timeout`) and the kill-latency bound.

### 2.3 F3 — reader-thread exceptions are swallowed (silent `""` output)

**Why it is a problem.** `text=True` readers decode strictly; a harness emitting
one invalid UTF-8 byte (binary spill into stdout, a truncated multibyte sequence at
a kill boundary — likelier than usual *because* the watchdog now SIGKILLs
mid-stream) raises `UnicodeDecodeError` **inside the reader lambda**
(`core/exec.py:283`). The thread dies, `out` stays empty, and
`out[0] if out else ""` (`exec.py:307`) converts the crash into an empty string.
Downstream, the turn parser sees no events, the oracle classifies a wrong failure
class, and nobody learns output was lost — a fail-loud violation. It is also a
**behavioral fork between watched and unwatched execs**: plain
`subprocess.run(text=True)` raises in the main thread. Since capture is now always
on in bench, every bench exec takes the watched path — the *quiet* one.

**Fix design.** Decode with `errors="replace"` on the watched `Popen` **and** the
unwatched `subprocess.run` (parity — telemetry wants best-effort text, not
strictness), and wrap the reader bodies to stash any unexpected exception and
re-raise it after `join()` in the main thread. Two lines each; the parity test
(V2) then pins watched/unwatched `CaptureResult` equality for a well-behaved child
so the seam can never grow a second personality again.

**Proven by:** V2's invalid-UTF-8 emitter row ("not silent" assertion) + the
watched/unwatched parity row.

### 2.4 F4 — `--no-save-captures` leaks captures on abort; `--capture-dir` conflict is silent

**Why it is a problem.** The flag's contract is "persist nothing." The temp root
(`suites/bench.py:276`) is removed only by a line at the *end* of `run_bench`
(`bench.py:766`) — success path only, not a `finally`. Bench runs are long and
abort often (Ctrl-C, provisioning failure, a gate bug); every abort strands full
wire captures — **prompts and completions included** — in `/tmp`, exactly the data
the user opted out of persisting. Separately, `--no-save-captures --capture-dir X`
silently discards `X`: the user believes captures land at `X`; neither the captures
nor a warning exist. Both are quiet contract breaks (Working Rule 8).

**Fix design.** Ownership, not cleanup-by-remembering: `run_bench` wraps everything
after `_setup_bench_capture` in `try/finally` (or registers the rmtree on the same
`ExitStack` pattern the cells already use), keyed on `not opts.save_captures` — the
multi-harness `run_benches` loop then inherits correctness because each `run_bench`
owns its own root. For the flag conflict, fail loud at the CLI boundary
(`cli.py`): `--no-save-captures` with an explicit `--capture-dir` is a contradiction
→ `log_err` + exit 2 (a hard error beats a warning here; there is no sensible
"both" semantics to guess).

**Proven by:** V4's completed-run and SIGINT-abort residue rows + the conflict row.

### 2.5 F5 — provenance records the config, not the per-cell resolved gates (and no harness version)

**Why it is a problem.** The triple doctrine makes gate caps *benchmarked config
dimensions*; DoR §6 promises "provenance records the **resolved** gate values per
cell." Dumping the raw `GatesConfig` once (`provenance.py:170`) means a reader must
re-run danno's resolution logic — *at the same danno version* — to know a cell's
effective caps; resolution itself can change between versions, so old runs become
unreconstructable. The missing **harness version** is the same class of gap with a
sharper edge: the sandbox template is unpinned and opencode's V1→V2 cutover changes
`agent.steps` semantics, so the polite-stop leg of option B can silently change
meaning between two runs whose provenance is byte-identical.

**Fix design.** Record where the value is *used*: put the cell's `ResolvedGates`
(three numbers) on each `BenchVerdict` row in `bench.json` (`_row` in
`suites/bench.py`) — verdict-local data survives partial runs and needs no join
logic; keep the raw `[gates]` block in `provenance.json` as the intent record.
Harness version: extend `harness_provenance` to exec `<harness> --version` in the
VM once per run (occ/claurst already have version constants; opencode is the one
that matters).

**Proven by:** V5's provenance-completeness assertions; the steps-semantics canary
is what makes the version field *actionable*.

### 2.6 F6 — README still documents bench-era `--capture`

**Why it is a problem.** Constitution, Documentation Hygiene: *"Any
behavior-affecting change MUST update affected `--help` text, READMEs, and related
documentation in the same commit. A documentation gap is a bug."* The `--help` text
was updated; `README.md:321` ("With `--capture`, `report.html` also plots …") now
describes a deprecated no-op, and `--no-save-captures` — the flag a user actually
needs — appears nowhere. A reader follows the README, passes a dead flag, and never
finds the opt-out.

**Fix design.** Docs-only, rides the F-fix branch: rewrite the bench sentences of
the capture section (always-on, why — it is the gate sensor — plus
`--no-save-captures` / `--capture-dir`), leaving the `sandbox start --capture` /
`validate --capture` paragraphs untouched (those paths are unchanged). No script;
verified by reading.

### 2.7 F7 — the reaper's `pkill -f` SIGKILLs its own wrapper

**Why it is a problem.** `_REAP_PATTERN` (`suites/base.py:115`) is delivered as
`bash -lc "pkill -9 -f 'opencode|claurst|…'"`; the wrapper bash's own command line
*contains the pattern text*, so pkill kills its parent shell (pkill skips only its
own PID). Today the damage is a spurious exit 137 absorbed by `check=False` — but
it means the reap exec **cannot distinguish "reaped fine" from "reap failed"**, and
any future edit that checks the exit code, adds a second command after `pkill`, or
reuses the pattern in a checked context inherits a live bug wearing a passing test.

**Fix design.** The classic self-exclusion bracket: first character of each
alternative in a character class — `'[o]pencode|[c]laurst|index[.]mjs|[D]ANNO_RELAY'`.
The regex still matches the real processes, but the wrapper's cmdline (which
contains the *bracketed* text) no longer matches itself. Keep `-f` (needed for
`index.mjs`, which runs under `node`); the reap exec should then exit 0, which V3
asserts.

**Proven by:** V3's post-kill invariants (reaper exit code 0 + no surviving
processes + next cell clean).

### 2.8 F8 — `HARNESS_NAMES` is dead code

**Why it is a problem.** Working Rule 2 (nothing speculative) — but the real cost
is drift: the same four names exist as inline `Literal[...]` types in
`BenchmarksConfig.harnesses` and `GatesConfig.harness`; an unused parallel tuple
invites someone to update one and not the others.

**Fix design.** Either delete the constant, or make it the single source: a
`HarnessName = Literal["opencode", "claurst", "occ", "claude"]` type alias used by
both fields (and the tuple derived via `get_args` if a runtime sequence is ever
needed). Pick whichever the follow-up branch touches naturally; do not keep both.
No script; the type checker is the net.

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

- **GV0 — DONE** (2026-07-15, branch `gv0-stub-ai-gate-validation` stacked on
  `docs-runaway-gates-validation`). Stub server + script engine
  (`src/book_em_danno/stubai/{script,server}.py`: `finish`/`tool_call`/`tool_loop`/
  `drip`, the six wire dialects of the §V1 table + Anthropic, discovery table,
  recorded-404 gap, transcript in the `capture.proxy` JSONL schema) + V1
  (`tests/test_gate_sensor_dialects.py`) + V2 (`tests/test_exec_watchdog_hostile.py`).
  `ninja check` green (653 passed, 5 strict xfails = F1×3 usage-less dialects, F2
  grandchild-holds-pipe, F3 invalid-UTF-8). `pytest-timeout` added (dev group). No
  Docker. Note: V2's F2 red row uses a **bounded** (2 s) grandchild so the red test
  latency-fails rather than actually hanging the fast gate — the plan's own
  "fail, never hang" rule applied to the test itself. *(≈ stub plan M0, scoped to gates.)*
- **GV1 — DONE** (2026-07-15, branch `gv1-runaway-gate-fixes` stacked on GV0 — Q1
  resolved in favour of a stacked follow-up PR, not amending #88, so the fix and its
  un-xfailed V1/V2 proof live together). F1 = path-based round counting
  (`capture/proxy.py` ticks by `capture.usage.is_inference_request`, not usage presence)
  + Ollama `eval_count`/`prompt_eval_count` mapping (`capture/usage.py`, JSON + NDJSON) +
  `wire_metrics.parse_capture_records` alignment (a usage-less round is a metric row with
  `None` tokens, so `request_count` == the tally) + `GateTally.blind()` fail-loud warning
  at cell end (`suites/base.py`). F2 = `start_new_session` + `killpg` process-group kill +
  bounded `reader.join` + daemon readers + loud truncation warning (`core/exec.py`). F3 =
  `errors="replace"` on both exec paths + surfaced reader exceptions. F4 = temp
  capture-root cleanup in a `finally` (`suites/bench.py`) + `--no-save-captures
  --capture-dir` conflict fails loud at the CLI (`cli.py`), with fast rows in
  `test_cli`/`test_validator_bench`. F6 = README bench-capture rewrite. F8 = `HarnessName`
  Literal alias replaces the dead `HARNESS_NAMES` tuple. All 5 GV0 strict xfails flipped
  green; `ninja check` green (660 passed, 0 xfailed). V4's in-sandbox residue rows remain a
  Tier B (GV3) item.
- **GV2 + GV3 — AUTHORED, NOT LIVE-VERIFIED** (2026-07-15, same branch
  `gv2-gv3-tier-b-slow-tests` stacked on GV1; user opted to write the Tier B suites
  without a live Docker run). Shared fixtures `tests/slow/gates_fixtures.py`
  (`scripted_backend` = stub on a fixed port behind the always-on capture proxy;
  `provisioned_sandbox` = one real sandbox wired to the proxy; `run_scripted_turn` = the
  `suites.base.run_cell` watchdog seam driving the harness `*_run`). V3
  `test_gates_termination_matrix.py` (harness ∈ {opencode, occ, claurst} × {clean-finish,
  runaway-loop, token-gate, wallclock-gate, next-cell-clean}, with option-B graceful-vs-
  external split + `surviving_harness_pids` post-kill invariant). V4
  `test_gates_lifecycle.py` (real `danno bench` subprocess: `--no-save-captures` residue on
  completed + SIGINT-abort). V5 `test_gates_drift.py` (opencode `steps` wire-round-trip
  canary that names a V1→V2 runner flip; provenance-records-gates; opt-in stub-vs-live SSE
  framing diff). All `-m slow` + `requires_docker` skip guard + `pytest-timeout` ceilings;
  20 tests collect, `ninja check` unaffected (660 passed, 31 deselected). **Every file
  carries a loud NOT-YET-LIVE-VERIFIED banner listing what the first Docker run must
  confirm (loop-tool name/args, occ/claurst relay routing, `agent.steps` honored).**
  Still TODO for a full GV2/GV3 close: the live run itself; F5 (per-cell resolved gates +
  harness version — V5's provenance row asserts loose until then); F7 (reaper self-kill
  bracket — V3 observes it); the bench-docs run-before-every-campaign note + retiring
  `live-verify-runaway-gates.md` §1–2.

## 7. Open questions

- **Q1 — F1 fix location: RESOLVED** (2026-07-15) — landed as its own branch
  `gv1-runaway-gate-fixes` stacked on GV0 (the recommended option), so #88's review
  surface stays stable and the fix ships together with its un-xfailed V1/V2 proof rather
  than split across the stack.
- **Q2 — cadence enforcement:** Tier B can't run in CI (needs Docker Desktop + host
  Ollama for V5's live diff). Proposal: a documented pre-campaign checklist step in
  the bench README, plus running it after any sandbox-template/fork bump. Automation
  beyond that (launchd/cron on the dev Mac) is out of scope unless asked.
- **Q3 — `pytest-timeout` vs hand-rolled deadlines:** dependency preferred (battle-
  tested thread-based kill); confirm it plays well with the sandbox-exec subprocesses
  at GV2.
