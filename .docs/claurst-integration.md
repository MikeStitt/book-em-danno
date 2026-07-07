# Claurst integration — findings, fixes, and resume plan

_Dogfooding `danno sandbox start --harness claurst` with a local Ollama model
(2026-06-25/26, ongoing). This doc is a **handoff**: it captures every confirmed
claurst↔danno↔Ollama integration issue we've hit, the instrumented build used to
diagnose them, the current-run analysis, and how to resume after a `/clear`._

Scope note: this file started as the agent-loop **stall/timeout** investigation and has
been broadened to track the **whole claurst integration** (loop stalls, turn limits, the
TUI, and the context meter). The model itself is fine: `qwen3-coder-next` Unsloth
`UD-Q4_K_M`, context-capped to 64K (`ollama create qwen3-coder-next-ud-q4-65k`), kept
resident (`keep_alive=-1`).

## TL;DR status

| # | Issue | Status |
|---|-------|--------|
| 1 | **45 s `provider_stall_timeout`** aborts slow-prefill streams → tool calls dropped → premature end-of-turn → "stall" | **ROOT CAUSE FOUND + FIXED (→600 s) + CONFIRMED** |
| 2 | "Hang every ~10 tool rounds / needs a continue" | **IDENTIFIED — it's claurst's `max_turns` default of 10. Not a bug; raise `--max-turns`.** |
| 3 | TUI **ghosting/overlapping text when scrolling** | **OPEN (upstream claurst TUI bug); workarounds only** |
| 4 | Status bar shows the **wrong context-window size** (128 K shown vs ~64 K real; also hits `nvidia/*` cloud models → 128 K fallback) | **ROOT CAUSE FOUND — models.dev catalog miss → provider fallback guess. Handled at runtime via `CLAURST_MODELS_PATH` (Fix A); Fix B (patch) deferred.** |
| 5 | Status bar's **"used" tokens never update** (frozen) | **FIXED + WIRE-VERIFIED — added `include_usage_in_stream: true` to the `ollama()` and `nvidia()` providers; rebuilt + installed; confirmed `stream_options.include_usage` now on the wire.** |
| 6 | Even with usage flowing, the **"used" accumulation over-counts** | **FIXED (built + installed) — `tui/src/app.rs` now *sets* `context_used_tokens` to the latest turn's prompt+output instead of `+=`.** |
| 7 | **Tools silently disabled** for catalog-miss / custom-registered models → model narrates tool calls as text → 1-turn end | **ROOT-CAUSED + WIRE-VERIFIED — claurst gates the `tools` array on the models.dev `tool_call` flag; fix = `"tool_call": true` in the `CLAURST_MODELS_PATH` entry (runtime).** |

Issues **1–3** are the agent-loop track; **4–6** are the context-meter track. They are
independent — an honest context meter needs all of 4, 5, and 6.

---

## Bug 1 — the 45 s `provider_stall_timeout` (PRIMARY, fixed)

**Mechanism (confirmed via wire capture + loop instrumentation):**
`query/src/lib.rs` has a stream-stall timer (`provider_stall_timeout`, hard-coded **45 s**)
that aborts the model stream if no SSE data arrives for 45 s, then retries twice
(`retries_left = 2`). This local model's **prefill (prompt processing) takes 98–157 s**
on large context (measured in the danno relay log: `-> upstream` to `<- upstream` gaps).
During prefill Ollama emits nothing, so the 45 s timer fires, retries exhaust, and claurst
builds the assistant turn from **incomplete** stream data — the tool-call blocks never
arrived → `tool_use_blocks = 0` → the loop treats it as "no tools" and returns
`EndTurn` → the TUI waits for the user → the "stall." A *submitted* message (any text,
even "a"/"ZZZTEST", which **is** injected as a user turn — verified in the capture)
restarts the loop. A bare keypress does **not** (it's the submit path that re-drives it).

**The fix:** bump the timer to 600 s. Two locations:
- `provider_stall_timeout` — OpenAI-compat / Ollama path (`from_secs(45)` → `from_secs(600)`)
- `STALL_TIMEOUT` const — Anthropic path (same)

**Confirmed working** (patched binary, loop trace): across a long run — **`STALL FIRED` = 0**,
**221 `CONTINUE`** (tool rounds auto-advanced), and turns survived `DISPATCH calling →
stream opened` gaps of **60–64 s** that the old 45 s would have killed.

**Not addressed by any upstream PR.** PR #187 ("variable timeout") makes the *reqwest
HTTP-client* timeout configurable, but that's **already 600 s** (`openai_compat.rs:117`)
and is a *different* timer — #187 does not touch `provider_stall_timeout`. PR #185
("hangs when a provider stream pauses between tool-call chunks") is the same family but a
different point in the loop. PR #189 (bash detached-child hang) is fixed in v0.1.6 and is
unrelated. **The real fix is making `provider_stall_timeout` longer/configurable** —
worth an upstream issue/PR.

---

## Issue 2 — `max_turns` default of 10 (config, not a bug)

The "it hangs every ~10 rounds and I have to type continue" symptom is **claurst's
max-turns guard**: `effective_max_turns` defaults to `MAX_TURNS_DEFAULT` (=10). At
`query/src/lib.rs:805` the loop checks `if turn > effective_max_turns { … return EndTurn }`
**before** building the request. So after 10 tool rounds it returns
"Reached maximum turn limit (10)" and waits for the user. The loop trace shows this as
`TURN 11 start: <N> messages` with no following `BUILD` — always **turn 11**, regardless
of message count (observed at 162/172/181/189/199 messages). Runtime is idle
(`do_epoll_wait`), resumes on submit. **Not a deadlock/panic.**

**Fix:** raise it — claurst accepts `--max-turns <N>` (a plain `u32`, `default_value_t = 10`).
There is **no "unlimited" sentinel**: `--max-turns 0` stops on the very first turn (`1 > 0`).
For effectively-unlimited pass `--max-turns 4294967295` (`u32::MAX`); for a safer cap that
still catches a runaway non-converging loop, pass e.g. `--max-turns 50`–`200`. (danno
forwards args after `--`: `… -- --dangerously-skip-permissions --max-turns 50`.) It is also
settable via the agent definition (`max_turns` in the agent `.md`, which **overrides** the
CLI) or config. Verify the flag against the installed claurst (`claurst --help | grep -i max-turns`).

---

## Bug 3 — TUI scroll redraw artifacts (UI fix needed, OPEN)

When the terminal window is **scrolled** during a claurst session, the TUI **ghosts**:
fragments of longer lines bleed into the right side, line-number/content interleave
(e.g. `2d file`, `3ers/mikeChains…`). This is **incomplete cell-clearing on scroll
repaint** in claurst's **ratatui** TUI (vacated cells not blanked) — an upstream claurst
v0.1.5/0.1.6 bug, **not** danno (danno just `exec -it`s the binary). Reproduced in Ghostty
(host `TERM=xterm-256color`, VM has the terminfo); not a terminal/locale issue.

**Workarounds:** `Ctrl-L` (force full redraw), resize the window a hair (`SIGWINCH` →
repaint), avoid mouse-wheel scrolling the window (use claurst's own scroll if any). The
real fix is upstream: *"ratatui TUI leaves ghost/overlapping text when the terminal is
scrolled — vacated cells not cleared on scroll repaint."*

_(Side note: the danno sandbox VM runs in a **non-UTF-8 locale** — `LC_CTYPE=POSIX`,
charmap `ANSI_X3.4-1968`. Pass `--env LC_ALL=C.utf8` for cleaner Unicode box-drawing.
Durable fix = danno injecting a UTF-8 locale in the agent env at launch.)_

---

## Bugs 4–6 — the context meter (wrong window + frozen/over-counting "used")

The bottom status bar shows `used / window` as a context gauge. **Both halves are wrong**
for a local Ollama model, for two independent reasons, and the "used" math is suspect even
once it flows. claurst **never queries Ollama for context info** — there is no `/api/show`
call; everything comes from a static catalog and the stream's `usage`.

### Bug 4 — wrong window size (the denominator)

The window comes from a static **models.dev catalog** keyed by `provider/model`
(`api/src/model_registry.rs`), resolved in `tui/src/app.rs:1870` `refresh_context_window_size()`:

```rust
if let Some(entry) = self.model_registry.get(provider, model_id) {
    self.context_window_size = entry.info.context_window;   // from catalog
} else {
    self.context_window_size = match provider {             // fallback guess
        "anthropic" => 200_000, "openai" => 128_000,
        "google" => 1_048_576, _ => 128_000,
    };
}
```

`ollama/qwen3-coder-next-ud-q4-65k` is a locally-created model → not in models.dev →
`get()` returns `None` → the `_ => 128_000` arm. **So the bar reads 128 K when the model is
`num_ctx`-capped to ~64 K.** (Note: even querying Ollama's `/api/show` would report the
*architecture* `context_length` — qwen3-coder's full 256 K — not the `num_ctx` you pinned;
the value you actually want is the cap, which **danno already knows**.)

**Fix A — no rebuild (recommended).** claurst loads an overlay cache that `extend()`s the
catalog, with an env override that wins outright (`cli/src/main.rs:1090`,
`CLAURST_MODELS_PATH`). `load_cache` parses models.dev `api.json` shape; `limit.context`
maps straight to `context_window`. Drop a tiny file and point the env at it:

```json
{
  "ollama": {
    "id": "ollama", "name": "Ollama", "env": [],
    "models": {
      "qwen3-coder-next-ud-q4-65k": {
        "id": "qwen3-coder-next-ud-q4-65k",
        "name": "Qwen3 Coder Next (65K)",
        "limit": { "context": 65536, "output": 8192 }
      }
    }
  }
}
```

Launch with `--env CLAURST_MODELS_PATH=/path/to/that.json`. `ModelRegistry::new()` still
loads the full bundled snapshot first, then this one entry overlays on top →
`get("ollama","qwen3-coder-next-ud-q4-65k")` returns 65536. **Natural danno feature:**
danno created the `…-65k` model and knows the cap, so it can generate this JSON + inject
the env var at launch.

**Fix B — patch (general).** Make `refresh_context_window_size` (and the sibling
`message_utils::context_window_for_model`, which separately defaults to 200 K) read the
effective `num_ctx` from Ollama `/api/show` **`parameters.num_ctx`** for `ollama/*` models,
falling back to the catalog. Upstream-worthy.

### Bug 5 — "used" counter never updates (the numerator) — Ollama-side VERIFIED

The bar renders `context_used_tokens`, which **only** advances inside `TurnComplete` when
`usage` is present (`tui/src/app.rs:5982`):

```rust
if let Some(ref u) = usage {                       // ← None for Ollama
    let turn_tokens = u.input_tokens + u.output_tokens
        + u.cache_creation_input_tokens + u.cache_read_input_tokens;
    self.context_used_tokens = self.context_used_tokens.saturating_add(turn_tokens);
}
```

`usage` is `None` because the **Ollama provider never requests streamed usage**. Sending
`stream_options:{include_usage:true}` is gated behind a per-provider quirk
(`openai_compat.rs:505`), and the `ollama()` provider doesn't set it
(`openai_compat_providers.rs:67` — `include_usage_in_stream` falls to its `false` default
via `..Default::default()`). So claurst never sends `stream_options` → Ollama emits no
usage chunk → `usage = None` → the counter is frozen at 0. Matches the relay log exactly
(zero `usage`/`prompt_tokens` anywhere).

**Verified Ollama-side** (tiny request through the in-VM relay, model already resident):

```
$ curl …/v1/chat/completions -d '{… "stream_options":{"include_usage":true} …}'
data: {…"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":1,"total_tokens":10}}
```

→ Ollama **does** return usage the moment the flag is sent. **Fix:** one line in the ollama
quirks —

```rust
    no_api_key_required: true,
    include_usage_in_stream: true,   // ← add this
    ollama_native_host: Some(host),
```

(Needs a rebuild; no env knob for it.)

### Bug 6 — "used" accumulation over-counts (secondary)

Even once usage flows, `app.rs:5985` does `context_used_tokens += (input + output + cache)`
**every turn**. But `input_tokens` already counts the *entire growing history* each turn,
so summing across 10+ agentic turns massively over-counts — the meter would balloon past
the window and read >100 %. A correct gauge should **set** it to the latest turn's
`prompt_tokens` (+ that turn's output), not accumulate.

### Behavior caveat for the whole meter

`context_window_size` / `context_used_tokens` feed the **TUI gauge / status line only**.
claurst does **not** auto-compact in interactive mode (0 compaction markers across the long
run; it never summarizes — message count only ever drops at full conversation restarts).
So fixing 4/5/6 makes the gauge *honest* (you can watch it approach the real 64 K), but it
does **not** add compaction or change run behavior. `is_context_limit()` /
`ContextWindowExceeded` are about provider *error* responses, not this local estimate.

---

## Bug 7 — tools silently disabled for unknown/custom-registered models (the big one)

**Symptom:** a NIM spike (qwen3-next-80b, glm-5.1, deepseek-v4-flash) had **all three** models
end after 1 turn (`ENDTURN`, `CONTINUE=0`, no file written), each emitting tool calls as
**plain text in its native format** (qwen narrated ` ```bash Glob…``` `; glm `</think><tool_call>…/>`;
deepseek `<｜DSML｜invoke name="Glob">…`). Looked like "models can't drive the harness."

**It was none of those.** Direct NIM probes proved NIM returns clean structured `tool_calls`
in **both** streaming and non-streaming whenever sent a standard OpenAI tools array. The gap is
claurst-side, and precise:

- `query/src/lib.rs:1115` `let mut caps = provider.capabilities();` — for openai-compat the
  default is `tool_calling: true` (`openai_compat.rs:1260`).
- `query/src/lib.rs:1116-1124` — **if the model is in the model registry, override**
  `caps.tool_calling = model_entry.tool_calling` (the models.dev `tool_call` bool).
- `query/src/lib.rs:1125` `let provider_tools = if caps.tool_calling { … } else { Vec::new() };`
  → when false, **the `tools` array is omitted entirely.** The model then only learns about
  tools from the **system-prompt prose** ("You have access to … Glob patterns …") and narrates
  them; NIM's tool parser never engages (no `tools` field) → `tool_use_blocks=0` → 1-turn end.

**The footgun is the interaction with Bug 4.** A custom/cloud model isn't in the bundled
models.dev catalog, so:
- *No `CLAURST_MODELS_PATH`* → registry miss → caps stay at provider default → **tools ON**, but
  context **window wrong** (Bug 4). (This is why the local Ollama run worked — custom model,
  no registry entry, default `tool_calling=true`.)
- *`CLAURST_MODELS_PATH` without `tool_call`* → entry parses with `tool_call` defaulting to
  `false` → override → **tools OFF**. ← registering the model to fix the window *disabled tools*.
- *`CLAURST_MODELS_PATH` with `"tool_call": true`* → **tools ON + window right.** ← the fix.

**Fix (runtime, no rebuild):** add `"tool_call": true` (and `"reasoning": true` for thinking
models) to each model in `scratch/nvidia-models.json`. **Wire-verified:** captured request now
carries `tools` (45 defs, `tools[0]=Bash`) and qwen3-next emits a real structured `Glob` call.

_(Captured via a tiny env-gated dump patch — `CLAURST_DUMP_REQUEST=<path>` writes the exact
outgoing openai-compat request body; `openai_compat.rs` just before send. Diagnostic only.)_

This is upstream-worthy: claurst should treat a registry **miss** as "use provider default
capability," not silently disable tools, and/or warn when it sends zero tools to a tool-capable
agent. As-is, any model not in the bundled catalog is a silent-no-tools landmine.

## Spike — NVIDIA NIM models as the "right AI" (fix-then-spike)

Hypothesis: claurst is a fine multi-turn harness; the local `qwen3-coder-next` UD-Q4 @ 64K
just doesn't converge (reads files forever). Test it against stronger cloud models on
**build.nvidia.com** (`integrate.api.nvidia.com/v1`, OpenAI-compat).

**Wiring (all VERIFIED):**
- claurst ships an `nvidia` provider (base `…/v1`, reads `NVIDIA_API_KEY`, registers only
  when the key is non-empty). Run as `-m nvidia/<nim-model-id>` (the NIM id itself contains
  a `/`, e.g. `nvidia/qwen/qwen3-next-80b-a3b-instruct`; claurst splits on the first `/` →
  provider=`nvidia`, model=`qwen/qwen3-next-80b-a3b-instruct`).
- **Egress works:** sandbox → `https://integrate.api.nvidia.com/v1/models` = HTTP 200 in
  0.6 s through the proxy (no allowlist work). `/v1/models` lists 121 live models without a
  key (used to pick exact slugs).
- **Provider path verified** with a dummy key → `[nvidia] Authentication failed` (a real 401
  from NIM, not a wiring error). Only a real `NVIDIA_API_KEY` is missing.
- **Bug 1 (45 s stall) is moot for cloud** (sub-second TTFT); the only loop knob the spike
  needs is `--max-turns`. **Bug 5** applies to NVIDIA too (now fixed in the build).

**Models chosen** (live, tool-calling-capable, user-selected 3-model matrix):
`qwen/qwen3-next-80b-a3b-instruct` (256 K — cloud twin of the local model, the control),
`z-ai/glm-5.1` (128 K — flagship agentic coding), `deepseek-ai/deepseek-v4-flash`
(1 M — removes context as a variable). Window sizes registered in
`scratch/nvidia-models.json` (→ `CLAURST_MODELS_PATH`) so the meter is honest for `nvidia/*`.

**Launch (within the existing danno sandbox `danno-temp-claurst-project-claurst`):**
```bash
docker sandbox exec -it danno-temp-claurst-project-claurst bash -lc '
  export NVIDIA_API_KEY=nvapi-REAL
  export CLAURST_MODELS_PATH=/Users/mikestitt/projects/temp/claurst-project/scratch/nvidia-models.json
  export CLAURST_LOOP_LOG=/tmp/claurst-spike-qwen3next.log
  claurst -m "nvidia/qwen/qwen3-next-80b-a3b-instruct" --dangerously-skip-permissions --max-turns 50'
```
(Swap the model id + log name for GLM-5.1 / DeepSeek-V4-Flash. Add `-p "<task>"` and drop
`-it` for a reproducible headless autonomous run; the danno Ollama relay in the container is
unused for cloud, so compare via `CLAURST_LOOP_LOG`, not the relay log.) Each `claurst`
invocation is a fresh session, so the three models can run sequentially in the same sandbox.

## Spike results (task: "agree to constitution.md; analyze book-em-danno/README.md for accuracy to the code; write results to scratch/*.md")

**Run 1 — before the Bug 7 fix (tools off):** all three models ended after **1 turn**, no file
written, each narrating tool calls as text in its native format (qwen ` ```bash Glob…``` `,
glm `</think><tool_call>…/>`, deepseek `<｜DSML｜invoke…>`). Pure Bug-7 artifact.

**Run 2 — after `"tool_call": true` (tools on):**
- **qwen3-next-80b — converged ✅.** 76 s, 8 tool rounds (`CONTINUE=8`, `ENDTURN=1`), wrote
  `readme-analysis.md` + `constitution-analysis.md`. The thing the local Ollama qwen3 never did.
- **glm-5.1 — tools worked, infra-failed.** 5 tool rounds (`[Read…]` executing), then died on
  `[nvidia] 503 ResourceExhausted: All workers are busy` (NIM capacity, retryable — left as-is).
- **deepseek-v4-flash — ran long** (12+ tool rounds, passed turn 11 so `--max-turns` fine);
  outcome non-isolated (see caveat).

**Methodology caveat (matters):** all three ran in the **same workspace** (`cd $WORKSPACE_DIR`),
sharing one `scratch/` and one `book-em-danno/` checkout, and the harness *renames* each model's
outputs into that shared `scratch/`. So later models can see earlier ones' analyses → only
**qwen3-next (ran first, clean scratch) is a valid independent result**; glm/deepseek aren't
rigorous. A proper matrix needs per-model isolation (fresh scratch + `git restore book-em-danno`,
outputs moved *out* of the tree between runs).

**Output-quality finding (the deeper lesson):** converging ≠ doing the work well.
qwen3-next's output was a confident **all-✅ rubber-stamp with no evidence** — 4 Read / 2 Glob /
**0 Grep / 0 Bash**, and it read **zero `src/**.py`** files, yet declared the README "aligns
perfectly with the actual codebase" (a claim it never verified — the task was *accuracy to the
code*). Its README claims are at least grounded (those topics do exist in the README), but its
**constitution analysis is partly hallucinated**: it invented generic sections ("team
communication and decision-making", "defined roles and responsibilities", "conflict resolution")
and missed every distinctive feature (two-tier advise-vs-`--apply`, `ninja check`, ADOS,
non-destructive installs). So: **the harness is viable; "the right AI" must be tool-capable *and*
rigorous.** `qwen3-next-80b-a3b-instruct` cleared the first bar, not the second, on this task.

## Cross-model comparison — Claude models (Claude Code subagents, host repo, not sandboxed)

To control for the harness, the **same task** (verbatim, only the output dir specialised)
was run through **Claude Code subagents** with a per-model override, in the real
`book-em-danno` repo (not a danno sandbox), each writing to an isolated `scratch/<model>/`
and read-only elsewhere. Then every model's specific claims were **fact-checked against the
code**. Baseline state tagged `readme-analysis-baseline` (= `dbd256d`).

| Model | Tools | Self-verdict | Fact-check result |
|---|---|---|---|
| **haiku** | 27 | "100% correct (49 claims)" | **rubber-stamp** — falsely claimed the README documents `sandbox update` (it doesn't); only trivial cosmetic findings; missed every real issue. (Did read code — cites are real — but affirmed completeness without checking.) |
| **sonnet** | 89 | accurate, 5 findings (M1–M5) | **all true, zero false claims** — most exhaustive; but missed F1. |
| **opus** | 24 | highly accurate, 5 findings (F1–F5) | **all true + caught the unique real bug (F1)**; best signal/noise. |
| **fable** | — | — | **unavailable** (service-gated; not run). |
| haiku output placement | — | — | wrote to the session scratchpad, not `scratch/haiku/` — minor instruction-following slip. |

**Claims verified against the repo:**
- `sandbox update` **exists** (`cli.py:623 @sandbox_app.command("update")`) but README has **0**
  mentions → sonnet M3 / opus F2 correct; **haiku wrong** (claimed it documented).
- `danno.toml.example:2` references `danno config generate`, which has **no CLI command** →
  **opus F1 correct & unique** (haiku + sonnet missed it). Real doc bug.
- `.gitignore` ignores `scratch/`, `.danno-validator/`, `.danno/` but **not**
  `.danno-bench/`/`.danno-benchmark/` → **opus F4 correct & unique**. Real repo gap.
- README `Kuberwastaken` vs code `kuberwastaken` (`claurst.py:45`) → sonnet M5 correct.
- `XDG_CONFIG_HOME={home}/config` subdir (`sandbox.py:425`) → sonnet M4 / opus F5 correct.
- pyproject `version = 0.9.0`, scripts `danno` + `book-em-danno` → opus correct.

### Six-model synthesis + failure-mode taxonomy

| Model | Converged | Read code | False claims | Caught the F1 bug | Net |
|---|---|---|---|---|---|
| qwen3-next (NIM) | ✅ | ❌ 0 files | yes (all-✅ + hallucinated constitution) | ❌ | worst |
| deepseek-v4 (NIM) | ✅ | ✅ deep | yes (false "missing": npm/reasoning_effort) | ❌ | rigorous but flawed |
| haiku (CC) | ✅ | ✅ some | yes (`update` "documented") | ❌ | weak rubber-stamp |
| sonnet (CC) | ✅ | ✅ exhaustive | none | ❌ | strong |
| **opus (CC)** | ✅ | ✅ efficient | none | ✅ | **best** |
| fable | — unavailable — | | | | |

Failure modes cluster: **false positives** ("all correct") = qwen3-next, haiku; **false
negatives** ("X missing" when documented) = deepseek-v4; **clean** = sonnet, opus — only
**opus** caught the genuine bug. Quality ≠ convergence: every available model drove the loop
to completion (claurst with Bug 7 fixed, and Claude Code), but rigor varied enormously, and
the two real repo bugs (`danno config generate` reference; `.gitignore` gap) were surfaced
**only by opus** across six models. **Re-run recipe:** `git checkout readme-analysis-baseline`,
give a model the prompt in that tag's annotation, then fact-check its claims against the code.

## The instrumented build (how to reproduce / rebuild)

- **Source clone:** `/Users/mikestitt/projects/temp/claurst-src` (claurst `main` tip,
  v0.1.6+). Patched file: `src-rust/crates/query/src/lib.rs`.
- **Patches applied** (current installed binary):
  - `query/src/lib.rs`: `loop_log()` helper (flushed trace to `$CLAURST_LOOP_LOG`);
    `provider_stall_timeout` + `STALL_TIMEOUT` **45 → 600 s** (Bug 1); trace points
    `TURN N start` · `BUILD` · `DISPATCH calling create_message_stream` ·
    `DISPATCH stream opened` · `PARSE turn=N …` · `STALL FIRED` · `CONTINUE` · `ENDTURN`.
  - `providers/openai_compat_providers.rs`: `include_usage_in_stream: true` on **both**
    `ollama()` and `nvidia()` (nvidia had no quirks block) — **Bug 5**, wire-verified.
  - `tui/src/app.rs` `TurnComplete`: `context_used_tokens` **set** to latest
    prompt+output instead of `+=` — **Bug 6**.
- **Deferred (not yet patched):** Bug 4B — read `/api/show` `parameters.num_ctx` for
  `ollama/*` (window currently handled at runtime via `CLAURST_MODELS_PATH`); MAXTURNS /
  panic-hook traces.
- **Build (Linux aarch64, Docker, ~1 min warm / ~15 min cold):**
  ```bash
  cd /Users/mikestitt/projects/temp/claurst-src
  docker run --rm --platform linux/arm64 -v "$PWD":/src -w /src/src-rust \
    -v claurst-cargo-target:/tmp/cargo-target -v claurst-cargo-reg:/usr/local/cargo/registry \
    -e CARGO_TARGET_DIR=/tmp/cargo-target rust:1-bookworm bash -c '
      set -e; apt-get update -qq >/dev/null
      apt-get install -y -qq cmake g++ golang-go clang libclang-dev libasound2-dev pkg-config >/dev/null
      cargo build --release -p claurst 2>&1 | tail -12
      cp /tmp/cargo-target/release/claurst /src/claurst-linux-arm64'
  ```
  (Deps matter: BoringSSL via `btls-sys` needs **cmake + g++ + clang/libclang-dev**;
  audio dep `cpal` needs **libasound2-dev + pkg-config**. The two cache **volumes** make
  retries fast.)
- **Install into the sandbox (via the workspace mount):**
  ```bash
  cp /Users/mikestitt/projects/temp/claurst-src/claurst-linux-arm64 \
     /Users/mikestitt/projects/temp/claurst-project/scratch/claurst-patched
  docker sandbox exec danno-temp-claurst-project-claurst bash -lc \
    'install -m0755 /Users/mikestitt/projects/temp/claurst-project/scratch/claurst-patched /home/agent/.local/bin/claurst'
  ```
  Version stays `0.1.6`, so danno's installer **skips** reinstall and preserves the patched
  binary. Verify with `grep -a -c "DISPATCH turn=" /home/agent/.local/bin/claurst`.
- **Run with tracing on:**
  ```bash
  danno sandbox start --apply --harness claurst -m ollama/qwen3-coder-next-ud-q4-65k \
    --env CLAURST_LOOP_LOG=/tmp/claurst-loop.log --env DANNO_RELAY_LOG=/tmp/danno-relay.log \
    -- --dangerously-skip-permissions
  ```
  (single line — multi-line paste mangles in zsh). Read:
  `docker sandbox exec danno-temp-claurst-project-claurst bash -lc 'cat /tmp/claurst-loop.log'`.

---

## Current-run analysis (the long run with many continues)

From `/tmp/claurst-loop.log` (1359 lines) + relay:
- **`STALL FIRED` = 0** across the whole run — the 600 s fix held.
- **221 `CONTINUE`**, only **2 `ENDTURN`** (model genuinely answered), **2** `tool_use_blocks=0`
  (= those 2 ends), **221** `tool_use_blocks≥1` (every other turn the tools parsed cleanly
  — **no drops**).
- Every "hang" = `TURN 11 start` with no `BUILD` → **max_turns (10)**; each `TURN 11`
  is immediately followed by a fresh `TURN 1 start` (your continue). Context grew to ~208
  messages (it never compacts; tool results get trimmed by the result-budget but message
  count keeps climbing). The run ended cleanly at `turn=10 stop=end_turn`.
- Relay: **327 / 327** balanced; longest prefill survived = **64 s**.
- **Context used at end:** no `usage` in the relay (Bug 5), so size is estimated from
  request body bytes — final request **208 messages / ~119 KB ≈ ~30–34 K tokens**, i.e.
  **~half** the real 64 K window. **No compaction** anywhere (Bugs 4–6 confirmed: meter is
  display-only and was both wrong-window and frozen-used during this run).

Interpretation: with the 600 s fix, the loop is healthy — it just keeps hitting the
10-turn cap, and the model spent all turns *reading files* without converging to a final
write (a model/agentic-discipline matter, separate from these bugs).

---

## Plan — additional instrumentation (if resuming the dig)

Issue 2 (max-turns) is **already explained**, so further instrumentation is mostly to
(a) make the trace self-explanatory and (b) catch any *real* large-context failure:
1. Add `loop_log("MAXTURNS turn=N: limit reached, returning")` at the max-turns return
   (`query/src/lib.rs` ~line 805) so it's not mistaken for a hang.
2. Add a **panic hook** (`std::panic::set_hook`) that appends panic message+location to
   `$CLAURST_LOOP_LOG` — to catch any genuine large-context panic in request assembly.
3. Finer traces in the loop-top→`BUILD` gap (after queue drains, after
   `apply_tool_result_budget`, after `api_messages` build, after `build_todo_nudge` /
   `build_system_prompt`) — only needed if a *non-turn-11* hang ever appears.
4. Rebuild (fast, cache warm), reinstall, reproduce.

**Untested side theory (relay):** danno's in-VM relay does `CONN close` after every
response while speaking HTTP/1.1 keep-alive; could in principle stall reqwest on a stale
pooled connection. Cheap test (no rebuild): make the relay send `Connection: close`
(`driver.py` `_OLLAMA_RELAY_SOURCE`) and re-run. Low priority — not observed once the
45 s + max-turns explanations landed.

---

## Resume plan (after `/clear`)

1. Read this doc + the memory pointer `[[resume-claurst-stall-investigation]]`.
2. State: patched claurst (600 s + traces) is **installed in the sandbox**
   `danno-temp-claurst-project-claurst`; source + warm build cache at
   `/Users/mikestitt/projects/temp/claurst-src`; cache volumes `claurst-cargo-target`,
   `claurst-cargo-reg`.
3. **Quick wins to try / land:**
   - Run with `--max-turns 50` (issue 2) and confirm the model runs to a real answer.
   - **Context meter (Bugs 4–6):** wire Fix A (`CLAURST_MODELS_PATH` JSON) for the right
     window with no rebuild; then in the next rebuild add `include_usage_in_stream: true`
     to the ollama quirks (Bug 5) and switch the used-token `+=` to set-to-latest (Bug 6).
     Optionally do Bug 4B (read `/api/show` `num_ctx`) instead of Fix A for a general patch.
   - Decide whether to land the 600 s fix as an **upstream claurst issue/PR** (make
     `provider_stall_timeout` configurable) — our patched binary is the PoC. Same for the
     Ollama context-window/usage gaps (Bugs 4/5).
   - File the **TUI scroll-redraw** bug upstream (bug 3).
4. danno-side follow-ups (separate from claurst): inject a **UTF-8 locale** in the agent
   env at launch; generate the `CLAURST_MODELS_PATH` catalog entry automatically from the
   known model cap; consider the relay `Connection: close` hardening.
5. The capture-wiring + relay-trace + 0.1.6-bump work already merged/PR'd (PR #57 merged;
   PR #59 open) — unrelated to these claurst bugs but part of the same dogfooding.
