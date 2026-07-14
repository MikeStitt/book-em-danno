# `danno bench` runaway gates — design of record

**Status:** IMPLEMENTED 2026-07-14 (see "Implementation status" below) · **Date:**
2026-07-14 · **Author:** Claude (Opus 4.8), from a live review of the capture/telemetry/
bench code with the user.

## Implementation status

Implemented + unit-tested (branch `docs-bench-runaway-gates`):

- **M0** — `GateLimits`/`GatesConfig`/`resolve_gates` in `suites/config.py`.
- **Sensor** — `GateTally` (`capture/gate.py`) fed by the proxy on each usage-bearing
  response; usage extraction lives in `capture/usage.py`, the single source of truth that
  `telemetry/wire_metrics` also imports (deduped after the Responses-API fix landed on main).
- **Enforcement** — the watchdog in `core/exec.py` (`Runner.watching()` +
  `_capture_watched`), wired per cell in `suites/base.py:run_bench_task`; killed cells
  become `runaway`/`over-budget`/`timeout` verdicts (`oracle.gate_verdict`).
- **Always-on capture** — bench always runs the proxy; `--capture` deprecated to a no-op;
  `--no-save-captures` runs the proxy but persists nothing.
- **Native polite-stop caps (option B, §3.2)** — the harness's own cap is set to the
  resolved `max_turns` (occ/claurst `--max-turns`; opencode `agent.steps` at the
  harness-level value, seeded once), while the external watchdog kills at `max_turns +
  grace` (`watchdog_max_turns`, grace = `max(3, 10%)`). So a cap-honoring harness stops
  GRACEFULLY first; the kill fires only on overshoot.
- **Provenance** — the resolved `[gates]` config is recorded in `provenance.json`.

No material deferrals remain. (opencode's `agent.steps` is set at the harness level, not
per-cell — per-cell precision stays the external kill's job, since the config is seeded
once; that is a deliberate simplification, not a gap.)

Supersedes the "runaway guard" sketch in
[`plan-stub-ai-test-harness.md`](plan-stub-ai-test-harness.md) §4 knob 2 / M2
(`cell_timeout_s + normalized max_turns`). That plan stays the **test harness** that
TDDs this feature; this file is the **feature design**.

---

## 1. Problem — why a wall-clock cell timeout is the wrong *primary* gate

`danno bench` runs one **cell** = one `(harness × model × task)` permutation = a single
headless turn (`base.py:run_bench_task`), which is the harness's *entire internal agent
loop* over one exercise. Some harnesses never bound that loop:

| harness | internal cap | enforced? |
|---|---|---|
| occ | `--max-turns 30` (always passed) | ✅ hard |
| claurst | ~10 (its own default; danno passes nothing) | ✅ by claurst |
| claude | none passed | Claude Code default |
| **opencode** | **`agent.steps` — soft cap only** (asks the model to stop, doesn't force it; §3.4); default unset = uncapped | ❌ **the runaway** (observed 383 tool_calls / 2314 s) |

There is also **no `subprocess` timeout** on the exec seam (`core/exec.py:161`
`subprocess.run(...)` has no `timeout=`), so a pathological opencode cell hangs
`danno bench` forever.

The obvious fix — a wall-clock `cell_timeout_s` — has a fatal flaw as the *primary*
gate: **the right value varies wildly across the matrix.** Cold local 32B vs paid
frontier vs free-but-throttled NVIDIA differ by ~100× in seconds-per-round. Any single
timeout is either too tight for the slow-but-legitimate case or too loose to catch the
fast runaway, and tuning it per-model pushes exactly the reasoning onto the user that a
"clean" design was supposed to remove. **Wall-clock conflates "slow" with "stuck."**

## 2. The insight — gate on units that are invariant across the matrix

The axes that vary across the matrix are **speed** and **price**. The unit that does
**not** vary with model speed is **round count**: 50 rounds is 50 rounds whether each
takes 2 s or 2 min, local or cloud, free or paid. So the primary runaway detector should
count rounds, not seconds. Total tokens are the invariant unit for the *spend* concern.
Wall-clock is demoted to catching the one thing counts can't see: a single request that
never returns.

This yields **three gates**, each measuring the unit appropriate to what it guards:

| gate | unit | guards against | speed-invariant? |
|---|---|---|---|
| **Gate 1 — round cap** | inference-call count | non-converging agent loop (opencode 383-round tail) | ✅ |
| **Gate 2 — token cap** | tokens per cell | runaway *token spend* on paid/cloud models | ✅ |
| **Gate 3 — wall clock** | seconds | a *wedged* cell (provider stall, cold-load hang) — no rounds tick, no tokens flow | ❌ (but doesn't need to be) |

**Gate 3's tuning objection dissolves** once it is demoted. It no longer detects
runaways — Gate 1/2 do. Its only job is "kill a process that is truly stuck," so it is
set **absurdly loose** (a "nothing on Earth should legitimately take this long" value)
and needs **no per-model thought**. The per-model tuning burden only existed because the
timeout was doing the primary job; move that job off it and it becomes set-once-forget.

## 3. Enforcement — one watchdog, fed by the always-on capture proxy

### 3.1 Capture becomes always-on (this is what makes Gate 1/2 universal)

The capture proxy already sees **one `/v1` inference request per agent-loop round** and
records each response's `usage` (`capture/proxy.py`, `telemetry/wire_metrics.py`
`request_count` / `_extract_usage`). If the proxy is always in front of every
redirectable backend, danno gets a **live per-cell tally** (rounds + tokens) for free —
including for opencode, whose loop is otherwise unobservable. So:

- **Deprecate `--capture`.** In `danno bench` the recording proxy is **always on** (it is
  the gate sensor, not just an artifact recorder). Passing `--capture` warns "deprecated:
  capture is always on in bench" and is otherwise a no-op.
- **Split interception from persistence.** The proxy always intercepts and maintains an
  in-memory `GateTally`; writing the JSONL / metrics / transcript **sidecars** to disk
  becomes the opt-**out**:
  - `--no-save-captures` (default: save) — proxy still runs and feeds the gates, but no
    per-permutation artifacts are persisted. (See §7 Open decision D1 for the exact flag
    spelling.)
  - `--capture-dir <path>` stays: where sidecars land when saving (default
    `<out>/captures/`).
- **Overhead is now accepted, not opt-in.** The proxy buffers each response, adding a
  little latency to every cell. That was previously kept opt-in for "overhead honesty"
  (`bench-telemetry-features.md` §Cross-cutting); it is now the standing cost of
  universal gating. Record the fact (and the sampler interval, if `--sample`) in
  `provenance.json` so latency numbers stay interpretable.

**Uncaptured cells remain uncaptured.** `claude` (→ `api.anthropic.com`) and raw
`anthropic/*` opencode refs have no `base_url` lever, so the proxy never sees them
(`wiring.uncaptured_cloud_refs`, warned loudly today). For those cells Gate 1/2 have no
wire signal and only **Gate 3** applies; `claude` is the low-stakes reference row and
already carries its own `--model`/turn defaults, so this is acceptable. Fail loud about
it (Working Rule 8) at run start.

### 3.2 The unified watchdog

Replace the bare `subprocess.run(cmd)` at the captured-exec seam (`core/exec.py:161`,
used only by HUT turn execs) with a **watchdog-wrapped exec** used **only** for HUT cells
(default behavior everywhere else unchanged, per the stub-AI plan's knob 2 scoping):

```
spawn cmd via Popen
poll a GateTally (fed live by the cell's capture proxy) + wall clock, until exit:
    if tally.inference_calls  > watchdog_max_turns(resolved.max_turns)  -> kill, "runaway"
    if tally.tokens           > resolved.max_tokens                     -> kill, "over-budget"
    if elapsed                > resolved.timeout_s                      -> kill, "timeout"
record the breached gate + the partial transcript; fail loud in the report row
```

- **`inference_calls`** counts only usage-bearing `/v1` calls (chat-completions /
  responses / messages), matching `parse_capture_records` — discovery hits (`/api/tags`,
  `/v1/models`, title-gen) do **not** count as rounds.
- **Native "polite-stop" caps + a grace margin (option B).** The harness's own cap is set
  to the resolved `max_turns` (occ/claurst `--max-turns`; opencode `agent.steps`); the
  external Gate-1 kill sits a **grace margin above** it —
  `watchdog_max_turns(max_turns) = max_turns + max(3, 10%)`. So a cap-honoring harness
  (occ/claurst) **stops itself cleanly at `max_turns`**, well before the kill, giving a
  graceful shutdown + complete transcript; the external kill fires only for a harness that
  **overshoots** its cap — opencode (whose `agent.steps` is a *soft* ask, §3.4), a true
  runaway, or the small drift between the harness's step count and the proxy's
  inference-call count. The margin is why they aren't the *same* number: it guarantees the
  graceful stop wins the race. Gate 1 is uniform across harnesses because the always-on
  proxy counts rounds for all of them — no dependence on an opencode CLI flag that doesn't
  exist. (Gate 2/3 have no harness-side equivalent, so they are external-only, no margin.)
- **Kill + reap (verify, don't assume).** Killing the outer `docker sandbox exec` does
  **not** necessarily reap the relay + harness *inside* the VM. Implementation MUST
  confirm in-VM child cleanup and that the partial capture JSONL is flushed before the
  cell is torn down — this is a live-verification gate, not a code-review one.

### 3.3 Per-harness Gate 1 wiring summary

Native cap set to `max_turns`; external kill at `max_turns + grace`.

| harness | native cap passed? | external Gate-1 kill? |
|---|---|---|
| occ | ✅ `--max-turns <max_turns>` (`OCC_MAX_TURNS_FLAG`; defaults to 30 only when unset) | ✅ backstop at `+grace` |
| claurst | ✅ `--max-turns <max_turns>` (`CLAURST_MAX_TURNS_FLAG`; omitted → claurst's own ~10) | ✅ backstop at `+grace` |
| opencode | ⚠️ `agent.steps <max_turns>` (soft — asks, doesn't enforce; §3.4) | ✅ **primary** at `+grace` |
| claude | ❌ (uncaptured) | ❌ (Gate 3 only) |

### 3.4 opencode's `agent.steps` — a soft cap, not enforcement

opencode *does* have a native per-agent iteration cap — **`agent.steps`** (legacy alias
`maxSteps`, deprecated) — which danno can write into the generated `opencode.jsonc` as
opencode's polite-stop layer (the analog of occ/claurst's `--max-turns`). But it **asks the
model to stop; it does not force the loop to end**, so it can be opencode's *polite stop*
and never its *enforcement*. The external Gate-1 kill (§3.2) is what actually bounds
opencode. Specifics, from our source-level survey
([`plan-stub-ai-test-harness.md`](plan-stub-ai-test-harness.md) §8/§8.1, 2026-07-12; memory
`opencode-steps-advisory-responses-api`) and upstream docs:

- **Default is uncapped.** Upstream's own docs: *"If this is not set, the agent will
  continue to iterate until the model chooses to stop or the user interrupts"*
  ([opencode agents docs](https://opencode.ai/docs/agents/)) → `agent.steps ?? Infinity`.
- **V1 (what our sandbox template ships — opencode 1.16.2 — and still the live `opencode
  run` path on main):** at the cap opencode **injects a summarize-and-stop system prompt**
  (*"respond with a summarization of its work and recommended remaining tasks"*) and forces
  a text-only turn — a graceful *ask*, not a hard terminate. Verified advisory in practice:
  the proverb task passed under `steps=1` (memory `opencode-steps-advisory-responses-api`).
  Loop lives in `packages/opencode/src/session/prompt.ts` reading `agent.steps ?? Infinity`.
- **V2 (the ground-up session rebuild — NOT yet the live path, parity-incomplete):** as
  first written it hardcoded `MAX_STEPS=25` and *ignored* `agent.steps`, throwing a terminal
  `StepLimitExceededError` (anomalyco/opencode#30865); on main today parity was restored
  (honors `agent.steps`, unbounded when unset, injects `MAX_STEPS_PROMPT`, then *disables
  tools* after the cap — still a graceful settle, not a kill). The fix PR #31328 **died
  unmerged**; the change landed via another commit. The V1→V2 cutover is **parity-gated with
  checklist rows still missing**, so V2's step-cap behavior is neither shipped nor stable —
  do not depend on it, and **re-verify on every sandbox-template version bump**.
- **No CLI flag, and none is coming.** The exact ask — `--max-turns` on `opencode run` —
  was filed ([anomalyco/opencode#9869](https://github.com/anomalyco/opencode/issues/9869))
  *with* a working implementation (PR #13717); **both were auto-closed by the 60-day stale
  bot, never triaged/merged**. A `--timeout` ask (#3583) met the same fate. And
  `specs/v2/session.md` states outright that *"provider timeout, retry, and watchdog policy
  is intentionally deferred"* — upstream will never bound this at the runtime level.

**Bottom line:** danno should **set `agent.steps` = the resolved `max_turns`** as opencode's
best-effort polite stop (pending an M1 check that the installed release honors it), but the
**Gate-1 proxy-counter kill is opencode's only reliable bound** — precisely because the
native cap is soft in V1 and unfinished in V2.

## 4. Config — `[gates]` in `benchmarks.toml`

Gates are a **bench** concern (runaway protection during benchmarking), so they live in
`benchmarks.toml` (a validator concern, kept out of the provisioning `danno.toml` — see
`suites/config.py` docstring), not in `danno.toml`. **Global defaults need zero thought;
per-harness and per-model overrides are optional** (user decision, 2026-07-14).

```toml
[gates]                       # zero-thought global defaults (see §7 D2 for values)
max_turns  = 50               # Gate 1 — inference calls per cell
max_tokens = 2_000_000        # Gate 2 — total tokens per cell (universal wire signal)
timeout_s  = 1800             # Gate 3 — loose wedged-process backstop

[gates.harness.opencode]      # optional: opencode has no native cap → tighter round cap
max_turns = 40

[gates.model."o4-mini"]       # optional: a model that legitimately needs more rounds
max_turns = 80
```

**Resolution precedence** (most specific wins, per gate field, independently):
`[gates.model.<name>]` > `[gates.harness.<name>]` > `[gates]` > built-in default.
Fields are merged, not replaced wholesale — a per-model table that sets only `max_tokens`
inherits `max_turns`/`timeout_s` from the harness/global layers.

- **Per-model key** = the `danno.toml [models]` **name** (e.g. `o4-mini`), the stable
  human id, not the generic `<backend>/<tag>` dial ref.
- **Schema** (`suites/config.py`, `extra="forbid"` so a typo fails loud):
  ```python
  class GateLimits(BaseModel):          # every field optional at override layers
      model_config = ConfigDict(extra="forbid")
      max_turns:  int | None = None
      max_tokens: int | None = None
      timeout_s:  float | None = None

  class GatesConfig(GateLimits):        # global layer carries the resolved defaults
      harness: dict[Literal["opencode","claurst","occ","claude"], GateLimits] = {}
      model:   dict[str, GateLimits] = {}

  # BenchmarksConfig gains: gates: GatesConfig = GatesConfig(<defaults>)
  ```
- **CLI override** (optional, for one-off runs without editing the file): a `--gate
  max_turns=NN` style repeatable option MAY be added later; not required for v1. Out of
  scope unless asked.

## 5. Gate 2 details — the token spend cap

Gate 2 bounds **total tokens per cell** (`max_tokens`), and tokens only:

- Works **anywhere `usage` flows on the wire** — Ollama-local reports none, so the tally
  stays 0 and the gate is a natural no-op locally; NVIDIA / OpenAI-compat / o-series all
  report it. Robust, universal, needs no price data.
- **A token is a token** regardless of provider, region, currency, or tax, so `max_tokens`
  is invariant across every axis a runaway backstop must survive.

**A USD `cost_usd` facet was considered and deliberately dropped** (decision 2026-07-14,
after a pricing survey). A single dollar price does not hold across the matrix: the same
open model varies ~1.5–3× across providers (DeepInfra vs Fireworks vs Together), regional
cloud endpoints (Azure/Bedrock/Vertex) add 5–25% and bill in local currency, and
first-party APIs quote USD but add VAT/GST (and have begun local-currency pricing in some
countries). A price table would have to be keyed by `(backend, model)` **and** treated as
a ±30% estimate — accuracy a runaway *gate* does not need. `max_tokens` gives the same
protection with none of the drift, so it is the sole spend unit. If exact spend
*accounting* is ever wanted it belongs in **reporting** (where `ClaudeTurn.cost` /
`total_cost_usd` already parse vendor-reported cost post-hoc), never in a live kill gate.

## 6. Verdict & reporting

New `BenchVerdict.verdict` values (extend the oracle's turn classification):
`runaway` (Gate 1), `over-budget` (Gate 2), `timeout` (Gate 3). Each records **which
gate fired at what value**, keeps the **partial transcript** (the wire up to the kill),
and renders a **loud** report row (Working Rule 8 — a killed cell is never silently a
pass/fail). Provenance records the **resolved** gate values per cell so a cross-run
comparison knows what caps were in force.

## 7. Open decisions

- **D1 — disable-save flag spelling.** Proposed `--no-save-captures` (default: save).
  Alternatives: `--discard-captures`, a `--save-captures/--no-save-captures` pair. Pick
  one at implementation. *(Low stakes, easily changed.)*
- **D2 — default gate values.** Proposed `max_turns=50`, `max_tokens=2_000_000`,
  `timeout_s=1800`. These are runaway backstops, not fairness normalizers: set high enough
  that no legitimate solve hits them, low enough to catch the pathological tail (opencode's
  observed 383 rounds). Tune against real bench data before locking.
- **D3 — in-VM reap.** Confirm (live) that killing the outer exec reaps the in-VM relay +
  harness and flushes the partial JSONL. If not, add an explicit in-VM teardown.

## 8. Milestones (revises the stub-AI plan M0–M2)

- **M0** — `GatesConfig` schema + resolution + unit tests (pure, no Docker). Feed a
  malformed `[gates]` through the loader → fail loud; assert precedence merge.
- **M1** — always-on capture in bench: deprecate `--capture`, add the save opt-out, split
  proxy interception from persistence, expose the live `GateTally`. Verify the proxy
  feeds an accurate round/token count on a real local cell.
- **M2** — the watchdog: Popen + poll(tally, clock) + kill; thread resolved `max_turns`
  into occ/claurst `--max-turns` and opencode's `agent.steps` (soft polite-stop, §3.4);
  new verdicts + report rows + provenance. The opencode `tool_loop` red row (stub-AI plan
  §4) goes green: `danno bench` returns a loud `runaway` verdict instead of hanging —
  proving the Gate-1 kill works even though `agent.steps` alone does not enforce.

Each milestone is TDD'd against the stub AI in
[`plan-stub-ai-test-harness.md`](plan-stub-ai-test-harness.md) (the `tool_loop(forever)`
script is the D1/runaway fixture). `ninja check` green per milestone; each config knob
**exercised**, not just edited (Constitution "Configuration is Code").

## 9. Cross-references

- Capture mechanism / proxy / wiring: `src/book_em_danno/capture/{proxy,wiring}.py`.
- Wire parsing (round count, usage across chat-completions/Responses/Anthropic):
  `src/danno_validator/telemetry/wire_metrics.py`.
- Exec seam to wrap: `src/book_em_danno/core/exec.py:161`.
- Bench cell driver + verdict: `src/danno_validator/suites/base.py:run_bench_task`,
  `bench.py`.
- Per-harness turn drivers + native caps: `src/danno_validator/driver.py`
  (`OCC_MAX_TURNS_FLAG`, claurst `--max-turns` per line 74, `opencode_run` uncapped).
- opencode `agent.steps` soft-cap + upstream status (§3.4):
  [`plan-stub-ai-test-harness.md`](plan-stub-ai-test-harness.md) §8/§8.1, memory
  `opencode-steps-advisory-responses-api`; [opencode agents docs](https://opencode.ai/docs/agents/),
  [anomalyco/opencode#9869](https://github.com/anomalyco/opencode/issues/9869) (+ PR #13717,
  #3583, #30865 / PR #31328).
- Benchmarks config: `src/danno_validator/suites/config.py`.
- Test harness that TDDs this: [`plan-stub-ai-test-harness.md`](plan-stub-ai-test-harness.md).
- Matrix fairness / max-turns inequality background:
  [`benchmark-grading-harness-fidelity.md`](benchmark-grading-harness-fidelity.md) §5,
  [`nvidia-nim-free-tier-probe.md`](nvidia-nim-free-tier-probe.md).
