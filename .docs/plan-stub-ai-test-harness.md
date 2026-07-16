# Plan — Stub-AI test harness for harness-loop termination behavior

**Status:** DESIGN (2026-07-12, not yet implemented)
**Problem owner:** the *harness* leg of danno's triple (harness × model+config ×
sandbox) — see memory `danno-benchmarks-the-triple`.

## 1. The problem, with evidence

A non-interactive run (`danno bench`, `danno validate`) does **not terminate**
if the AI gets stuck in a loop emitting tool calls, because no layer of the
stack bounds the agent loop for every harness:

| Layer | Bound today | Reference |
|---|---|---|
| opencode agent loop | **none** — `opencode run` loops to completion | `.docs/aider-3task-cross-harness-runs.md` §"Conditions common to all runs": `--max-turns` is harness-level and UNEQUAL; opencode = uncapped |
| occ agent loop | `--max-turns 30` (always appended by `occ_run`) | same doc; `src/danno_validator/occ.py` |
| claurst agent loop | its own default ≈10 (danno passes none) | same doc; memory `resume-claurst-stall-investigation` |
| danno exec of the harness | **none** — `subprocess.run(cmd, ..., capture_output=True)` has **no `timeout=`** | `src/book_em_danno/core/exec.py:161` |
| claurst/occ relay | per-*read* timeout only (`DANNO_RELAY_TIMEOUT`, 3600 s). A looping AI that keeps answering **never trips it** | `src/danno_validator/driver.py:101-108` |
| `--capture` proxy | per-*upstream-read* 600 s only; same non-trip property | `src/book_em_danno/capture/proxy.py` (`_UPSTREAM_TIMEOUT_S`) |

Observed in the field (memory `nvidia-nim-free-tier-probe`, Finding 2): an
opencode cell on a pathological model hit **383 tool_calls**; on the paid
NVIDIA tier the estimated loop ceiling was ~$10–15 of spend per runaway cell.
"The runaway tail is structurally opencode's" (`aider-3task-cross-harness-runs.md`).
The only backstop today is a human at the keyboard.

Two distinct defects fall out:

- **D1 (hang):** with an unbounded harness (opencode), `danno bench` blocks
  forever at `exec.py:161`. Violates fail-loud: the run neither completes nor
  errors.
- **D2 (fairness):** turn caps are unequal across harnesses (30 / ≈10 / ∞), so
  cross-harness tool_call and latency columns aren't comparable. A normalized
  bench-level cap is itself a benchmarked config dimension (triple doctrine),
  to be recorded in `provenance.json` — never a silent patch.

Today we can't even *demonstrate* D1 deterministically: it needs a live model
that happens to loop. That is what the stub AI fixes.

## 2. Goal

A pytest-driven test harness that substitutes a **deterministic, scripted stub
AI** for the model backend, runs the real harness (opencode / occ / claurst /
claude where routable) against it in a danno-like environment, and **captures
the full wire transcript** of harness↔AI interaction so tests can assert on
turn counts, termination, and timing.

- **Phase 1:** non-interactive (`opencode run` / `-p` headless), the `danno
  bench`/`validate` path.
- **Phase 2:** drive the TUI side too (interactive harness in the sandbox),
  same stub on the AI side.

Non-goals: benchmarking model quality (stub replaces the model); testing
Ollama itself; testing cloud auth.

## 3. Architecture (Phase 1)

```
 host                                   │ Docker sandbox VM (proxy-only egress)
                                        │
 pytest ── danno driver (opencode_run…) ┼─ docker sandbox exec → harness (HUT)
   │                                    │        │ OLLAMA_HOST / OPENAI_BASE_URL
   │ fixtures                           │        ▼
   └─ StubAI server ◄── squid proxy ◄───┼── (claurst: 127.0.0.1 relay, unchanged)
        │  (host.docker.internal:PORT → localhost rewrite, --allow-host)
        └─ transcript.jsonl  ← every request+response, timestamped
```

The stub sits exactly where host Ollama sits today; every existing wiring path
(proxy `--allow-host`, `host.docker.internal` rewrite, claurst relay, occ
`OPENAI_BASE_URL`) is reused unchanged. Nothing about the sandbox or the
harness is mocked — only the model.

### 3.1 StubAI server — `src/book_em_danno/stubai/server.py`

`ThreadingHTTPServer` on an ephemeral port, same skeleton as
`capture/proxy.py` but **terminating** the wire: it answers from a script
instead of forwarding upstream. Precedent for shipping test infrastructure in
`src/`: `capture/proxy.py` (promoted from `tests/slow/capture_proxy.py`).

Wire dialects (only what the harnesses actually dial — pinned live per the M1
flag-pinning precedent):

| Endpoint | Dialect | Who dials it |
|---|---|---|
| `POST /v1/chat/completions` | OpenAI-compatible; SSE streaming + non-streaming | opencode (Ollama `/v1`), occ (`OPENAI_BASE_URL`) |
| `POST /api/chat` | Ollama native; NDJSON streaming | claurst (`OLLAMA_HOST`) |
| `GET /api/tags`, `/api/show`, `/api/version`, `/v1/models` | discovery/health | opencode startup + title-gen, claurst, occ |
| anything else | 404, **recorded** | self-documenting gap discovery |

Response *framing* is seeded from real `--capture` recordings (golden JSONL
from past bench runs) so SSE/NDJSON chunking is wire-faithful; a small opt-in
slow test diffs the stub's framing against a live Ollama capture to detect
drift.

Side traffic (title-gen — memory `opencode-titlegen-hits-local-model` — and
discovery endpoints) is answered from a static table **without consuming
script steps**, routed by model tag, so scripted turn counts stay exact.

### 3.2 Script engine — `src/book_em_danno/stubai/script.py`

A deterministic state machine: each inbound chat/completions request for the
model-under-test consumes the next step. Step vocabulary = the failure
taxonomy the oracle already classifies (`.docs/bench-telemetry-features.md`:
pass / stall / refusal / error / hallucinated-tool) plus transport faults:

- `tool_call(name, args)` — one well-formed tool call
- `tool_loop(n=None)` — answer **every** request with another tool call;
  `n=None` = forever. **The runaway reproducer.**
- `finish(text)` — final assistant text (`finish_reason: stop` / `done: true`)
- `stall_narrate(text)` — text, no tool call, no question (the classic stall)
- `hallucinated_tool()` — tool name absent from the request's advertised tools
- `malformed_tool_call()` — syntactically broken arguments JSON
- `slow(first_byte_s)` / `drip(tokens_per_s)` — latency injection (regression
  net for claurst's 45 s `provider_stall_timeout` class of bugs)
- `http_error(status)`, `disconnect()` — transport failures

Scripts are plain Python data in the test file — no DSL, no config files
(simplicity first).

### 3.3 Transcript capture — reuse the `--capture` JSONL schema

Every request and the stub's response are appended to `transcript.jsonl` in
the **same schema `capture/proxy.py` writes**, so
`telemetry/wire_metrics` tooling reads live captures and stub transcripts
interchangeably. Fixture handle API:

- `stub.transcript()` → parsed events
- `stub.completion_requests(model=...)` → agent-loop round-trip count
- `stub.port`, `stub.base_url`

### 3.4 pytest structure — two tiers

**Tier A — fast, in the `ninja check` gate, no Docker.**

- `tests/test_stubai_server.py` — script engine, SSE/NDJSON framing,
  side-traffic routing, transcript schema (client = plain `urllib` in-test).
- `tests/test_stubai_watchdog.py` — the danno-side guard (§4) unit-tested
  against a fake `Runner`, the established pattern of
  `test_validator_driver*.py` (everything routes through `Runner.capture`).

**Tier B — `-m slow`, requires Docker Desktop sandbox.**

The security invariant (memory `opencode-only-in-docker-sandbox`) is not
optional here: with a `tool_loop` script the harness **executes real tool
calls**, so the HUT must run inside the sandbox — the stub only controls what
the "model" says, not what the harness does about it.

- Fixture `stub_backend(script)` — starts the stub on the host; yields handle.
- Fixture `harness_cell(harness, stub)` — creates/wires a sandbox via the
  existing validator provisioning with `--allow-host localhost:<stub.port>`,
  points the harness at `host.docker.internal:<stub.port>` (rewrite path
  verified — memory `sbx-hdi-rewrite-verified` / legacy equivalent).
- `tests/slow/test_harness_termination.py`, parametrized harness × script:

| Script | Assertion |
|---|---|
| `finish` after 3 × `tool_call` | green path: harness exits 0, transcript shows exactly 4 round-trips, oracle verdict `pass` |
| `tool_loop(forever)` | **the D1 test.** occ exits with ≤30 completions; claurst with ≤ its default; opencode: the danno watchdog (§4) terminates the cell. In *all* cases `danno bench` returns, records a loud `runaway`/`timeout` verdict + partial transcript — never hangs |
| `stall_narrate` | oracle classifies `stall`; run terminates |
| `hallucinated_tool` / `malformed_tool_call` | harness-specific recovery observed and recorded; run terminates |
| `drip` slower than harness stall timeout | claurst stall-timeout regression net |

Every Tier B test carries a `pytest-timeout` ceiling so a regression **fails**
instead of hanging CI — the test harness must not itself have the bug it
exists to catch.

## 4. The fix the harness exists to TDD — danno-level runaway guard

Two knobs, both benchmarked config dimensions recorded in `provenance.json`
(triple doctrine — harness fixes are config, not silent patches):

1. **Normalized `max_turns`** (closes D2): bench/validate-level setting; pass
   `--max-turns N` to occ and claurst. opencode has **no CLI flag** (upstream
   #9869 closed NOT_PLANNED, PR #13717 closed unmerged — see §8), but it has a
   **documented per-agent `steps` config field** (docs/agents.mdx "Max steps";
   legacy alias `maxSteps` deprecated): when the limit is reached the agent is
   forced to answer with text (a summarize system prompt) — a graceful cap.
   danno already owns `.opencode/opencode.jsonc` generation, so it can emit
   `agent.<name>.steps` today. **V1-vs-V2 VERIFIED 2026-07-12** (source-level,
   tag `v1.16.2` = the version in the current sandbox template): the
   `session.prompt` route dispatches to `SessionPrompt`
   (`packages/opencode/src/session/prompt.ts`) whose loop reads
   `agent.steps ?? Infinity` — the **V1 path**, so `steps` is honored and the
   default is unbounded; the V2 runner (`packages/core/src/session/runner/
   llm.ts`, hardcoded `MAX_STEPS=25`) exists in the tree but is NOT wired into
   `opencode run` at this tag. Remaining M1 checks: (a) behavioral
   confirmation via the stub (config-is-code: exercise it, don't trust the
   source read); (b) no markdown agent def shadows `steps` (markdown beats
   generated jsonc on conflict — memory `opencode-agent-config-precedence`);
   (c) **re-verify on every template version bump** — the sandbox template is
   unpinned (driver.py pinned flags against 1.17.7 on 06-17; today's template
   ships 1.16.2) and the V1→V2 cutover is planned upstream (PR #31328's
   framing), after which `steps` handling changes.
2. **Runaway gates** (closes D1). The guard grew past a bare `cell_timeout_s`
   into a **three-gate model** — full design in
   [`plan-bench-runaway-gates.md`](plan-bench-runaway-gates.md): a round-count
   cap (Gate 1, speed-invariant, the primary detector), a token cap
   (Gate 2), and a **demoted** loose wall-clock backstop (Gate 3) for wedged
   processes only. Enforced by a watchdog wrapping the captured-exec seam
   (`exec.py:161`, HUT execs only) that polls a live `GateTally` fed by the
   now-always-on capture proxy + the clock, killing on the first breach and
   recording which gate fired with the partial transcript. `max_turns` is also
   threaded into occ/claurst native caps so they stop cleanly before the
   external kill. This is the M2 target below.

Until knob 2 ships, the opencode `tool_loop` row is the harness's standing
**red test** — that's the TDD sequencing, not a defect in the plan.

## 5. Phase 2 — driving the TUI side

Same stub AI and sandbox wiring; adds a TTY driver on the harness side.

- **Mechanism (recommended): tmux inside the sandbox.** `docker sandbox exec …
  tmux new-session -d -x 120 -y 40 '<harness tui cmd>'`, drive with `tmux
  send-keys`, observe with `tmux capture-pane -p`. Deterministic, fixed-size,
  snapshot-friendly; avoids pexpect-through-the-docker-CLI pty fragility
  (fallback option if tmux proves unavailable in the VM images).
- **`TuiDriver` fixture:** `.send(keys)`, `.screen()` (stripped pane text),
  `.await_text(pattern, deadline)`; assertions combine screen scrapes with the
  **stub transcript** (e.g. "after ESC, zero further completion requests
  within 10 s" — the wire, not the paint, is the truth signal).
- **Test cases from the known-bug inventory** (memory
  `claurst-auto-compact-armed`):
  - ESC during a stub `tool_loop` cancels the in-flight query (claurst Bug 9
    regression); Ctrl+C still works after ESC; Enter after ESC does not spawn
    a second query loop (transcript shows no interleaved double stream).
  - Auto-compact does not latch-fire on estimated context windows (Bug 8) —
    stub reports inflated `usage` to arm it on demand.
  - opencode TUI stays responsive/cancellable during a runaway `tool_loop`.

## 6. Milestones

- **M0** — stub server + script engine + Tier A tests. Gate green, no Docker.
- **M1** — Tier B fixtures + termination matrix against *today's* behavior.
  Documents the opencode red row; pins each harness's real wire dialect
  against the stub (404-discovery pass).
- **M2** — runaway gates (Gate 1 round cap / Gate 2 token cap / Gate 3 loose
  timeout backstop, per [`plan-bench-runaway-gates.md`](plan-bench-runaway-gates.md))
  → the opencode red row goes green; provenance records the resolved gates.
- **M3 (Phase 2)** — tmux `TuiDriver` + claurst ESC/auto-compact regressions +
  opencode TUI runaway test.

## 7. Assumptions & open questions

- **A1:** opencode's Ollama provider needs only `/v1/chat/completions` +
  discovery endpoints and no models.dev fetch that blocks startup in the
  proxy-only sandbox (it already runs there against real Ollama, so the
  request set is closed and the stub's 404-recording makes any gap
  self-documenting on first M1 run).
- **A2:** SSE/NDJSON framing seeded from existing captures is faithful enough;
  the live-diff slow test guards drift.
- **A3:** tmux is installable in the sandbox VM (apt works through the proxy —
  memory `sandbox-pip-install-works`); verify in M3 spike, pexpect fallback.
- **Q1:** should `cell_timeout_s` default on for `bench` (proposal: yes,
  generous default, e.g. 3600 s/cell) and off for `validate`? Decide at M2.

## 8. Upstream status (opencode, surveyed 2026-07-12)

Upstream repo is now `anomalyco/opencode` (`sst/opencode` redirects). The
termination gap is **known upstream and unfixed at the CLI level**; a config
lever exists instead:

- **#9869** `[FEATURE]: CLI option --max-turns` for `opencode run` — the exact
  ask ("Exits with an error when the limit is reached. No limit by default";
  a commenter cites CI-overspend + Claude Code parity). **Closed NOT_PLANNED
  2026-07-06 by the 60-day stale bot**, never triaged.
- **PR #13717** `feat(cli): add max-turns flag to run command` (closes #9869)
  — implementation existed; **closed unmerged by the stale bot**.
- **#3583** `opencode run --timeout=60s` — closed NOT_PLANNED (stale).
- **`agent.<name>.steps`** — the lever that DID land: documented per-agent max
  agentic-iterations cap ("If this is not set, the agent will continue to
  iterate until the model chooses to stop or the user interrupts" — upstream's
  own statement of the uncapped default). Graceful: last step injects a
  summarize-and-stop system prompt. Legacy alias `maxSteps` deprecated.
- **#30865 / PR #31328** — the V2 session runner hardcoded `MAX_STEPS=25` and
  ignored `agent.steps` (V1 reads `steps ?? Infinity`); issue closed
  "completed" but the fix PR closed unmerged. **Resolved for our version
  (2026-07-12): the sandbox template ships opencode 1.16.2 (checked live via
  `docker sandbox exec … opencode --version`), and at tag `v1.16.2` the
  `session.prompt` route → `SessionPrompt` (`packages/opencode/src/session/
  prompt.ts`) runs the V1 loop (`agent.steps ?? Infinity`) — V2's runner is
  present in `packages/core` but not on the `opencode run` path.** So today:
  `steps` honored, default unbounded. The V1→V2 cutover is planned upstream;
  re-check on version bumps.
- **Provenance gap (danno):** `provenance.json` `harness_versions` records
  only `{"harness": "opencode"}` — no version number — and the sandbox
  template is unpinned, so the opencode version can drift silently between
  runs (1.17.7 pinned in driver comments on 06-17 vs 1.16.2 in today's
  template). Recording the harness *version* in provenance should ride along
  with M2.
- Open model-loop *reports* (phenomenon, not fix): #26220 (loop after tool
  calls complete), #19267 (agent stuck in infinite loop), #21850 (hallucinated
  `oldString` edit loop), #31725, #35784 (read-file loop), #15533/#28543/#30443
  (auto-compaction loop family — claurst Bug 8's cousin).

Implication for §4: knob 1 gains an opencode arm (`agent.steps` via the
generated jsonc) if M1 verifies the installed release honors it; knob 2
(`cell_timeout_s`) remains the universal backstop danno cannot get upstream.

### 8.1 What the V1→V2 change actually is

"V2" is not a runner tweak — it is opencode's ground-up **Effect-native
rebuild of the whole session runtime**, developed in the open under
`specs/v2/` (`todo.md` opens: *"we need to work towards a launch of v2 so we
can get out of this rebuild phase"*). The pieces that matter to danno:

- **Agent loop moves and changes shape.** V1's loop lives in
  `packages/opencode/src/session/prompt.ts` (`SessionPrompt.loop`); V2's is
  `packages/core/src/session/runner/llm.ts` — durable event-sourced sessions
  (a `session_input` admission inbox, projected history, replayable events),
  a per-session serialized `SessionRunner`, one explicit `llm.stream` per
  provider turn.
- **Step-cap semantics across the change** (the termination behavior we care
  about):
  - *V1 (shipped, incl. our 1.16.2):* `agent.steps ?? Infinity`; last step
    injects a summarize-and-stop assistant message. Uncapped by default.
  - *V2 as first written:* hardcoded `MAX_STEPS=25`, `agent.steps` ignored,
    terminal `StepLimitExceededError` (#30865).
  - *V2 on main today (2026-07-12, verified in source):* parity restored —
    honors `agent.info?.steps`, **unbounded when unset**, injects
    `MAX_STEPS_PROMPT` on the last step, and post-cap enforcement is
    "tools are disabled after the maximum agent steps" (graceful settle, not
    an error). PR #31328 itself died unmerged; the fix landed via another
    commit (#30865 closed "completed").
- **No watchdog is coming.** `specs/v2/session.md` states it outright:
  *"Provider timeout, retry, and watchdog policy is intentionally deferred.
  The runner does not impose a universal provider-stream inactivity or
  absolute timeout."* So even post-V2, nothing upstream bounds wall clock —
  danno's `cell_timeout_s` stays necessary permanently, not as a stopgap.
- **Compaction is redesigned** (budget-estimated pre-turn compaction +
  exactly-one overflow-triggered retry; *"recovery never loops"*) — upstream's
  answer to the auto-compaction-loop issue family (#15533/#28543/#30443).
- **Config schema changes with it** (`specs/v2/config.md`): `agent` →
  `agents` map, agent `prompt` → `system`, deprecated `maxSteps` removed;
  **`steps` is retained** as the agent's "iteration budget". So `steps` is the
  durable lever, but danno's jsonc generator will need a V2-schema mode at
  cutover.

### 8.2 What we know about the switch to V2

- **Cutover is parity-gated, not scheduled.** `specs/v2/session.md` carries a
  canonical **"V1 Runtime Context Parity"** checklist — behaviors marked
  complete/partial/missing — explicitly *"still needed before the V2 runner
  replaces V1"*, updated in the PR that changes each status. Several rows are
  still partial/missing today. No dates anywhere; named owners in `todo.md`
  (agent-loop rework, data mode, server cleanup) show it's actively staffed.
- **On main, V1 is still the live path**: the `session.prompt` route →
  `SessionPrompt`, whose loop still reads `agent.steps ?? Infinity`
  (`prompt.ts:1178`); the V2 runner is exercised by core tests only. `todo.md`
  notes the first runner slice runs *"without bridging through legacy
  `SessionPrompt.loop`"*, and a V1→V2 "shadow bridge" republishes V1 events
  into the V2 store during the transition.
- **Consequences for this plan:** (a) the M1 behavioral check (`steps: N` →
  exactly N round-trips in the stub transcript) is the drift detector — assert
  on **wire round-trips, not exit semantics**, since at-cap behavior differs
  across versions (summarize-prompt vs tools-disabled vs the old
  `StepLimitExceededError`); (b) the M2 provenance version-recording is what
  tells us *when* a template bump flips the runner; (c) knob 2 is permanent
  (see the deferred-watchdog quote above).
