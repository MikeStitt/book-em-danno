# Research: interactive-TUI testing for `danno sandbox start`

**Status:** research / design-of-record — NOT a plan, NOT implemented.
**Audience:** the next Claude (and Mike) evaluating whether/how to build the `@slow` interactive-start test.
**Scope:** in-scope harnesses = **opencode, codex, claurst**. Claude Code is *out* of scope
(it cannot be pointed at an arbitrary AI). Real-AI leg (when we get there) = **local Ollama qwen
only** (`qwen3-coder-next-65k`, already in bench5).

Constitution v2.2.0 re-affirmed for this work (fail-loud #8, simplicity #2, surgical #3,
verify-by-exercising, `ninja check` green before push, feature branch never main).

---

## 0. The question, restated (the lowered bar)

We want a `@slow` pytest proving `danno sandbox start` works **end to end through a harness's
interactive code path** — not that the TUI is pretty. Concretely the test must show:

1. **the interactive code is actually exercised** (not the headless/`run`/`exec`/`-p` path);
2. **an AI is connected** (the harness reaches our server and completes a turn);
3. **a tool call executes** (the harness runs a tool round-trip);
4. **compaction is reached with a fake AI** (context management fires);
5. **danno sees what the AI did** (our capture/wire plumbing observes all of the above).

Explicitly *not* in scope: rendering fidelity, colours, layout, "prettiness". We are allowed to
**parse the terminal for text markers** and/or **read the wire** rather than re-draw the TUI.

The pivotal design principle carried over from `plan-stub-ai-test-harness.md` §5 holds and is now
**confirmed at source in all three harnesses**: **wire, not paint, is the truth signal.**

### 0.1 What "wire, not paint, is the truth signal" means

Two things a running harness produces can be observed:

- **The paint** — the bytes the harness writes to the *terminal*: an ANSI cursor/cell stream that
  positions each glyph on the alternate screen. It is what a human *sees*. It is
  **non-deterministic to parse**: literals are split across cursor-move escapes, re-drawn on every
  frame, reflowed by width, and churned by version. danno does not own it, does not schema it, and
  cannot rely on its layout.
- **The wire** — the HTTP traffic between the harness and the AI server: the inference
  requests (full message history each turn) and responses (content + `usage`), plus tool-call and
  tool-result round-trips. danno **captures this itself** through the capture proxy, writes it as
  **whole JSON records** (`capture/proxy.py` `_record`, one complete object per line, auth
  redacted), and already derives structured metrics from it (`wire_metrics.py`). It is the
  **contract danno actually plumbs**, and it is **identical in shape across all three harnesses**
  regardless of how each one paints.

**The principle:** assert against the *wire*, because every capability we care about leaves a
deterministic, already-structured, danno-owned trace there — *AI connected* = a completed
request/response pair; *tool call executed* = a tool-result round-trip; *context shrinks* =
`usage.prompt_tokens` deltas; *compaction fired* = a distinct summarization request. The *paint* is
at best a **secondary, best-effort confirmation** (and for opencode, not usable at all — §3). We
allocate a pty because the harness *demands* one to run its interactive code (§1); we do **not**
allocate it in order to read the screen back as our source of truth.

---

## 1. Headline finding — a PTY is unavoidable; grepping the paint is optional

The research set out to answer: *can the supported harnesses detect that their terminal doesn't
support a TUI and gracefully adjust (fall back to a usable non-interactive mode)?*

**Answer: no. None of the three degrades gracefully into a usable interactive mode without a real
pty.** Each one, on the interactive entry path, either hard-errors or crashes when stdin/stdout are
not terminals. So the **drive side** of the test cannot avoid allocating a pseudo-terminal.

| harness | interactive entry (argv) | tty gate on interactive path | no-pty behavior | override env? |
|---|---|---|---|---|
| **opencode** | bare `opencode` (`packages/opencode/src/cli/cmd/tui.ts:72`) | **none** on the *full* TUI — `createCliRenderer` (`packages/tui/src/app.tsx:194`) just needs raw mode | crashes / unusable (no fallback). The `--mini` path *does* guard: "requires a TTY stdout"/"controlling terminal" | none for full TUI |
| **codex** | bare `codex` (no subcommand; `main.rs:988-1001` → `run_interactive_tui`) | **hard** — `tui::init()` (`codex-rs/tui/src/tui.rs:391-398`) checks `stdin().is_terminal()` **and** `stdout().is_terminal()` before raw mode; also a `TERM=dumb` refusal (`main.rs:2270-2286`) | errors `"stdin/stdout is not a terminal"`, exits; no fallback | only `TERM=dumb` (to *refuse*, not to bypass) |
| **claurst** | bare `claurst` / `claurst -m ollama/<tag>` (`crates/cli/src/main.rs:801-834`, `run_interactive` @1607) | **none explicit** — `setup_terminal()` (`crates/tui/src/lib.rs:217-275`) calls `enable_raw_mode()` unconditionally | `enable_raw_mode()` → `tcsetattr` returns `ENOTTY`, error propagates, process exits; no fallback | none (`no is_terminal`/`atty` anywhere in cli/tui) |

**Consequence for the test design:**
- **Drive with a pty.** `pty.openpty()` (stdlib) or `pexpect`/`ptyprocess`. This is the one
  non-negotiable. The pty must back **both** stdin and stdout for codex (it checks both).
- **Assert off the wire and/or a raw-stream grep** — we do NOT re-render. The "don't redraw the
  TUI" instinct is correct, but it applies to the **assertion** side, not the **drive** side.

Note the pty is created **inside the sandbox VM** context: `danno sandbox start` issues
`sbx exec -it ...` (`sandbox.py` `_exec_session` @909-983, `-it` at 973). The `-it` already asks
sbx for a tty; the open question (Spike A) is whether that tty is honored end-to-end when the
*host* side of `sbx exec` is itself driven from a pty we allocate, vs. whether we must drive an
in-VM tmux.

---

## 2. Compaction is usage-driven in all three — a fake AI *can* trigger it

This is the second load-bearing finding: **every harness computes "how full is the context" from
the model-reported `usage` in the response**, not from a client-side estimate of the prompt. So a
scripted fake server that returns **inflated `usage`** will drive real compaction. And compaction
emits a **distinct extra summarization request on the wire** — the most robust, paint-free signal.

| harness | trigger source | threshold / window | wire signal (summarization request) | greppable UI marker |
|---|---|---|---|---|
| **opencode** | `lastFinished.tokens` (model usage), `overflow.ts:22-34` `isOverflow`, called `prompt.ts:1161-1168` | fires if `count >= usable` AND `model.limit.context > 0` AND `compaction.auto !== false` | prompt "Create a new anchored summary from the conversation history." + "Output exactly the Markdown structure…" (`core compaction.ts:161-168`); event `session.compacted` | TUI divider ` Compaction ` (`session/index.tsx:1446`) — but opentui grid, **grep-unreliable** |
| **codex** | `last_token_usage.total_tokens` from Responses `response.completed` (`history.rs:297-315`, `client.rs:2000-2025`) | `token_limit_reached` `context_window.rs:74-79` vs `auto_compact_token_limit`/`model_context_window` | `SUMMARIZATION_PROMPT` (`compact.rs:53,122`), `SUMMARY_PREFIX` | **`Context compacted`** (`replay.rs:189`) — codex is grep-friendly |
| **claurst** | cumulative SUM of model usage (`app.rs:5982-5985`); interactive **99%** path `main.rs:2995-3013` | `used_pct >= 99` vs models.dev window (or 128k unknown-provider fallback); **also** a 90% query-loop path (`compact.rs:519-526`, window 100k) | query-loop summariser: "concise yet thorough conversation summaries…" (`compact.rs:607`) → real `POST /v1/chat/completions` | `"Context 99% full — auto-compacting…"` (`main.rs:3013`); `"Context compacted to stay within limits."` (`lib.rs:1720`) |

**How a stub forces it:** report `usage.prompt_tokens`/`completion_tokens` (chat) or
`usage.total_tokens` (Responses) at/above the model's declared context window. Two knobs make this
deterministic:
- register/declare a **small context window** for the stub model so modest usage exceeds it, or
- have the stub **inflate usage** directly (our `stubai` already supports per-reply `prompt_tokens`
  override).

**claurst gotchas (from source, correcting prior memory):**
- **No "8192 window."** The `8192` (`core/src/lib.rs:4146`) is `effective_max_tokens` (output cap),
  unrelated to the context window. Fallbacks are 128k/200k/1.05M (interactive) and 100k/200k
  (query-loop).
- **`--no-auto-compact` does NOT suppress the interactive 99% path** — `auto_compact_enabled` is
  written (`main.rs:1717`) but **never read** in the 99% gate. Good for us (we *want* it to fire);
  worth noting so we don't try to toggle it off and get confused.
- **claurst speaks OpenAI-compat, not Ollama-native.** The `ollama` provider sets
  `base_url = {OLLAMA_HOST}/v1` and chats over **`POST /v1/chat/completions`** (SSE `data:` frames +
  final `usage`), NOT `/api/chat`. Model discovery uses `GET /api/tags` and `/api/show`. **Our stub
  must serve `/v1/chat/completions` + `/api/tags` for claurst** — not the Ollama-native `/api/chat`.
  (`stubai` already speaks chat/Ollama-native/Responses; we just point claurst at the chat dialect.)

---

## 3. Text markers per harness (for the optional paint-grep assertion)

If we want a *UI-side* assertion in addition to the wire assertion, these are the stable literals.
**Caveat:** all three render on the **alternate screen in raw mode**, so a raw stdout capture is an
ANSI cell/cursor stream — literals are present but may be **split across cursor-move escapes**.
Grepping is therefore best-effort; the wire is authoritative.

- **opencode** (opentui/Zig cell grid — **least grep-friendly**; logo is drawn char-by-char):
  prefer `opencode run --format json` / `OPENCODE_PRINT_LOGS` / `--log-level DEBUG` (stderr) / the
  wire summarization request. Do not rely on scraping the TUI paint.
- **codex** (ratatui — **most grep-friendly**): `>_ OpenAI Codex (v` (`session.rs:328-334`),
  the model line `/model to change` (**verified present** in v0.144.5 — use THIS as the composer-ready
  marker), `Working` / `to interrupt` (status_indicator_widget), `$ ` / `Ran` / `Running`
  (exec_cell/render), `Context compacted` (`replay.rs:189` — **verified**, painted on real
  auto-compaction). ⚠ **`Ask Codex to do anything` does NOT appear in v0.144.5** — the composer
  placeholder is a rotating tip (e.g. *"Improve documentation in @filename"*); do not use it as a
  marker. Also: a first-run **"Do you trust the contents of this directory?"** dialog blocks the
  composer until answered (`1`+Enter) or pre-seeded. **codex exec** (headless) has a stdin trap —
  hangs on "Reading additional input from stdin…" unless stdin is redirected `</dev/null`; irrelevant
  to the interactive path but a known footgun.
- **claurst** (ratatui): banner ` Claurst ` + `v{VERSION}` (`render.rs:1479-1480`), input prefix
  `> ` (`render.rs:466`), `esc interrupt` while streaming (`render.rs:2261-2262`), `thinking` /
  `Thinking` (`render.rs:75-76,1194`). **Tool calls render a *friendly title*, not the raw name**:
  `Running command` (bash), `Reading file` (read), `Writing file` (write), `Editing file` (edit),
  `Listing files` (glob/list), `Searching code` (grep) — raw `tool_name` shows only for
  unknown/MCP tools (`messages/mod.rs:1007-1045`). So to assert "a tool ran," match the **friendly
  title** or (better) the wire tool-call, not the tool's name.

**Recommendation:** make the **wire** the primary assertion for all five capabilities; use a paint
grep only as a *secondary* confirmation, and only for codex/claurst where markers are clean. For
opencode, skip paint entirely and assert on the wire.

### 3.1 Do all three harnesses speak JSON + TUI? — No; the `--format json` trap

**No harness emits structured JSON in interactive mode.** The interactive path of all three is
**paint-only** (opentui/ratatui alternate-screen ANSI). Their only machine-readable output lives in
a *different, headless* subcommand:

| harness | interactive mode | structured/parseable mode | same code path? |
|---|---|---|---|
| **opencode** | bare `opencode` → opentui TUI (paint only) | `opencode run --format json` (`run.ts` headless) | **No** — different subcommand |
| **codex** | bare `codex` → ratatui TUI (paint only) | `codex exec` (headless; its own output) | **No** — different subcommand |
| **claurst** | bare `claurst` → ratatui TUI (paint only) | `claurst -p/--print` (headless text) | **No** — different flag/mode |

So **`opencode run --format json` diverges twice**, and using it for this test would defeat its
purpose:

1. **It's headless, not interactive.** `run` is opencode's non-TUI subcommand — it never touches
   the interactive code path (capability #1). Asserting on its JSON proves the *headless* plumbing
   works, which the existing Tier-B `gates_fixtures` headless-turn tests already cover.
2. **It has no codex/claurst analog.** codex's structured output is `codex exec`; claurst's is
   `-p`. These are three *different* headless code paths with three *different* output formats.
   Leaning on `--format json` would make the "one test across three harnesses" non-uniform and
   force per-harness output parsers.

**The only representation that is (a) produced by the *interactive* path and (b) *uniform* across
all three harnesses is the wire.** That is precisely why "wire, not paint" is load-bearing here: it
is the single truth signal that survives both the interactive-vs-headless split *and* the
per-harness format divergence. `--format json` / `codex exec` / `claurst -p` remain useful only as
*separate* headless sanity checks (already covered), never as the interactive test's signal.

---

## 4. What danno already owns (the seams the test asserts against)

The test does not need new production plumbing; it exercises existing seams. Confirmed present:

- **Capture proxy** — in-process `ThreadingHTTPServer`, records wire JSONL by seq, auth redacted
  (`capture/proxy.py` `_CaptureServer` 95-126, `capture_proxy()` 234-260, `_REDACT` 42).
  *(Issue #112: eager 0-byte file create at 244-246 — lazy-create fix proposed, not required for
  this test but would make "did this backend get dialed" a clean signal.)*
- **Env-forward by NAME** — `env_forward_argv` forwards `OPENAI_API_KEY` etc. by name (the #99 fix);
  for the fake/local leg we forward nothing secret.
- **Egress allow-list** — sbx `balanced` + allow ONLY the stub/Ollama host (fail-loud invariant:
  never `"**"`).
- **Context estimation from the wire** — each inference request carries full history, so response
  `usage.prompt_tokens` = current context size. `metrics_from_files` →
  `TurnWireMetrics.ctx_growth` (prompt_tokens per call), `ctx_deltas` (per-round deltas, **negative
  on compaction/shrink**), `peak_ctx_tokens` (`telemetry/wire_metrics.py` 44-63, 66-91, 192-201).
  → The **"context gets smaller" demo is deterministic**: script the stub to report *descending*
  `prompt_tokens` across rounds and assert `ctx_deltas` contains a negative value. This is
  independent of, and complementary to, triggering real compaction.
- **Transcript view** — `render_transcript` (214-244) proves "danno sees what the AI did."
- **stubai** — scriptable fake AI (`ToolCall`/`Finish`/`ToolLoop`/`Drip`), per-reply
  `prompt_tokens` override, speaks chat / Ollama-native / Responses-SSE / Anthropic; JSONL
  transcript in capture-proxy schema (`stubai/{script,server}.py`).
- **Reusable Tier-B harness** — `tests/slow/gates_fixtures.py`: `scripted_backend`,
  `provisioned_sandbox`, `run_scripted_turn`/`run_turn_for`, `loop_tool(harness)`,
  `PROXY_PORT=11455`/`STUB_PORT=11456`/`MODEL_TAG="stub"`. **These are headless-turn fixtures**;
  the interactive test needs a *new* pty-drive fixture alongside them (Spike A/B decides its shape).
- **Skip guard** — `sandbox_runtime_down()` probes the resolved runtime (`sbx ls`), not standalone
  docker (`tests/slow/sandbox_runtime.py:19`).

---

## 5. Python TUI-testing library landscape (the user-side toolbox)

Evaluated for "drive a real interactive process's pty and assert on what came back," under the
constraint that the harnesses are **Go/Rust/TS binaries running inside a sandbox VM** — NOT Python,
NOT Textual.

| tool | what it does | fit here | verdict |
|---|---|---|---|
| **`pty` (stdlib)** | `openpty()` → master/slave fds; drive any child on a real pty | zero deps, exactly the primitive all three harnesses require | **primary candidate** for the drive side (esp. if we drive `sbx exec -it` from the host) |
| **`pexpect`** (+ `ptyprocess`) | spawn on a pty, `expect()` patterns, `send()` | ergonomic pty driver; `expect` handles the ANSI-split-literal problem via regex/timeout | **primary candidate**; new dev dep (pure-python, light) |
| **`pyte`** | in-memory VT100 screen emulator — feed it the raw ANSI stream, read back a rendered character grid | solves "literal split across cursor moves": render then grep the grid, without a real display | **strong add-on** for the assertion side when we *do* want a paint grep (codex/claurst) |
| **tmux via raw `sbx exec`** | run tmux *inside the VM*, `send-keys` / `capture-pane` | in-container terminal emulator; capture-pane gives an already-rendered grid; matches `plan-stub-ai-test-harness.md` §5 TuiDriver design | **primary candidate** for the drive side **if** the host-pty→`sbx exec -it` path is flaky (Spike A vs B) |
| **`libtmux` / `hecate`** | python control of a tmux server | **can't reach an *in-VM* tmux from the host** (no socket across the sbx boundary) | **rejected** for in-VM use |
| **`vhs`** (charm) | golden-file terminal recording | golden/prettiness-oriented — the exact thing we're told NOT to evaluate | **rejected** |
| **Textual `Pilot` / `pytest-textual-snapshot`** | drive/snapshot Textual apps | harnesses are Go/Rust/TS, not Textual | **N/A** |
| **`mcp-tui-test`** (ref impl) | pexpect + pyte reference for testing a TUI over a pty | the exact pattern (drive pty, render with pyte, assert grid) — good prior art to mirror | **reference** |

**Shape that falls out:** `pexpect` (or stdlib `pty`) drives; `pyte` renders *only when* we want a
paint assertion; the **wire (capture JSONL + `wire_metrics`)** carries the primary assertions. tmux
`send-keys`/`capture-pane` is the fallback drive mechanism if the host-pty path doesn't survive the
`sbx exec -it` boundary.

---

## 6. The drive-mechanism fork (recommendation + the spike that settles it)

Two ways to get a pty in front of the harness, which runs **inside the VM** under `sbx exec -it`:

- **Option A — host-side pty into `sbx exec -it`.** We `pty.openpty()` (or `pexpect.spawn`) the
  `danno sandbox start …` invocation on the *host*; `-it` (already in `_exec_session`) forwards a
  tty into the VM; the harness sees a terminal. Simplest, exercises the *real* danno launch path
  verbatim. Risk: whether `sbx exec -it`'s tty allocation is fully honored when its own stdin/stdout
  are a pty we own (nested pty semantics) — **unverified**.
- **Option B — in-VM tmux.** `danno sandbox start` (or a thin exec) launches the harness inside an
  in-VM **tmux**; we drive it with `sbx exec … tmux send-keys` and read `capture-pane`. Robust,
  gives a pre-rendered grid, matches the §5 TuiDriver design. Cost: adds tmux to the VM toolchain
  and a layer that isn't part of the *real* `danno sandbox start` UX (assumption A3 in the plan —
  "tmux-in-VM" — is still **unverified**).

**Recommendation: prove Option A first; keep Option B as the documented fallback.** A is the higher
fidelity ("test what danno actually ships") and the cheaper if it works. The decision is entirely
empirical — hence Spike A below. **This fork is not yet locked**; it's the one open decision the
plan needs before it can be finalized.

### 6.1 Where partial buffers get merged so a grep can succeed

**First: the *primary* assertions never touch a partial buffer.** They read the **wire**, and the
capture proxy writes **whole JSON records** — one complete object per line via `_record`, never a
fragment. Grepping/parsing the capture JSONL is fragment-free by construction; nothing to merge.

Partial-buffer merging is a concern **only** for the *optional secondary paint grep*, because a pty
read stream fragments a marker two independent ways:

1. **Chunk fragmentation (byte level).** Each `os.read(master_fd, N)` / stream poll returns an
   arbitrary slice; `Ask Codex to do anything` can span several reads.
2. **ANSI interleaving (render level).** Even a fully-reassembled raw stream has cursor-move escapes
   *between* glyphs (the TUI positions each character), so a naive substring search over raw bytes
   fails regardless of how much you accumulate.

Both are handled in **one place: a `TuiDriver` / pty-reader component** (the `.send` / `.screen` /
`.await_text` shape from `plan-stub-ai-test-harness.md` §5). It owns the read loop and is where the
merge happens — and *which* merge depends on the drive option:

- **Option B (in-VM tmux) — the merge is free.** `capture-pane -p` returns an **already-rendered
  character grid**: tmux *is* the VT emulator, so it has already applied both the chunk reassembly
  and the ANSI cursor moves. `.await_text(marker)` just polls `capture-pane` and substring-matches
  the grid. No accumulation code, no pyte. (This is a genuine point in Option B's favor.)
- **Option A (host pty) — the merge is ours.** The `TuiDriver` must:
  1. **accumulate** every chunk into one growing buffer in a read loop (this is exactly what
     `pexpect.expect()` does internally against `.before`/`.buffer`, with a timeout — so if we use
     `pexpect` we get chunk-merge for free);
  2. **render** the accumulation through a **`pyte` `Screen`** to apply the cursor moves, then
     substring-match the grid — *or*, for simple single-line markers, strip ANSI with a regex and
     match the residue (fragile; pyte is the robust choice).
  `.await_text(marker, timeout)` = "keep reading + rendering until the marker appears on the grid or
  we time out."

So: **merge location = the `TuiDriver` read loop; merge mechanism = tmux `capture-pane` (Option B)
or accumulate-then-`pyte` (Option A).** Spike G decides whether the paint grep earns the `pyte`
dependency at all, or whether the wire assertions alone are sufficient and the paint grep can be
dropped (the current hypothesis: wire alone suffices; paint is a nice-to-have for codex/claurst).

### 6.2 Can we send prompts/commands into the TUI? (input injection)

**Yes — and it's the natural, same-channel complement to the pty we already must allocate.** The
interactive composer reads keystrokes from the terminal in raw mode; "typing a prompt" is simply
**writing bytes to the pty master** (Option A) or **`tmux send-keys`** (Option B). We can drive
exactly the kind of prompts asked about:

- *"What is in this folder?"* → the AI issues **one read/list tool call** (list/glob/`ls`) → a
  single tool round-trip on the wire.
- *"Write a python program that prints 100! and run it."* → the AI issues a **write** tool call then
  a **run/bash** tool call (a multi-step tool loop), the program executes **inside the sandbox VM**,
  and the factorial result flows back → a write→exec loop on the wire, plus real in-sandbox tool
  execution (danno's isolation path — safe, and worth exercising).

**Recipe (single-line prompt — the safe form):**
1. `.await` the composer being ready (startup marker on paint, or simply the harness's first idle
   state); do **not** send before it, or keystrokes are dropped.
2. Write the prompt **text only** (no trailing newline) to the pty / `tmux send-keys -l "<text>"`.
3. Send **Enter separately** as a bare carriage return `\r` (0x0D) / `tmux send-keys Enter`.
4. `.await` the turn starting **on the wire** (the first inference request appears in the capture
   JSONL) — the authoritative "the prompt was received and dispatched" signal.

**Stub leg vs real-AI leg — what the prompt text actually does:**
- **Stub (`stubai`):** the fake AI does **not** interpret the prompt — it replays *scripted* replies
  (`ToolCall` / `ToolLoop` / `Finish`). So for the stub leg the **prompt text is cosmetic**; what we
  prove is that the **keystroke → composer → dispatch → wire** path works and a scripted tool loop
  round-trips. We script `ToolLoop(write, then bash)` to *mirror* the factorial example
  deterministically, regardless of the literal prompt typed.
- **Real-AI leg (local Ollama qwen):** the prompt text genuinely drives behavior — qwen reads
  *"What is in this folder?"* and chooses a list tool; reads the factorial prompt and does the
  write→run loop itself. This is where the two example prompts are *semantically* exercised. Add it
  only after the stub leg is green.

**Caveats to spike (input encoding is the real unknown, not feasibility):**
- **Bracketed paste + keyboard-enhancement flags.** All three enable special input modes on entry —
  claurst confirmed at source: `EnableBracketedPaste` + `PushKeyboardEnhancementFlags` (Kitty
  keyboard protocol) at `crates/tui/src/lib.rs:240-249`; codex (ratatui/crossterm) and opencode
  (opentui) enable comparable modes. Consequences: (a) a **multi-line** paste may need
  bracketed-paste wrappers (`ESC[200~ … ESC[201~`) or each embedded `\n` could submit/mangle —
  hence the "text first, bare `\r` after" recipe for single-line prompts; (b) under the Kitty
  protocol, Enter may be reported as a key *event* rather than a bare `\r`, though `\r` is the
  baseline encoding most composers still accept. **This is the one genuinely uncertain part of
  input injection — verify it, don't assume it.**
- **Submit key differs from newline-in-field.** Composers often treat newline-in-text as "insert a
  line" and submit only on a discrete Enter — so keep prompt text and the submit keystroke separate.
- **Readiness race.** Send only after the composer is up; the wire-first assertion (step 4) also
  guards this — if no inference request appears, the keystrokes didn't land.

**Bottom line:** sending commands over the TUI is **feasible via the same pty/tmux channel** and is
in scope for the test. The feasibility is not in doubt; the **exact input encoding per harness**
(bracketed paste / Kitty-Enter) is the thing to pin down — see Spike H.

---

## 7. Spikes (do these before writing the plan/test)

Each spike is small, throwaway, and answers exactly one unknown. Run them in the sandbox
(fail-loud: allow only the stub host; never `"**"`).

- **Spike A — nested pty through `sbx exec -it`.** From the host, `pty.openpty()` + spawn
  `danno sandbox start --harness codex` (codex = the strictest gate: checks stdin *and* stdout).
  Assert the process does **not** exit with `"stdout is not a terminal"` and reaches
  `Ask Codex to do anything`. **If green → Option A is viable; if red → Option B (tmux).** This is
  the single most decision-critical spike.
- **Spike B — in-VM tmux availability & capture.** `sbx exec <vm> which tmux` (is it in the base
  image?); if present, `tmux new -d 'codex'` + `capture-pane -p` and confirm we can read
  `Ask Codex to do anything`. Confirms A3 and unblocks Option B. If tmux is absent, note the
  toolchain add cost.
- **Spike C — stub drives real compaction, observed on the wire.** Point **one** harness (start with
  **codex** — cleanest markers + Responses `total_tokens`) at `stubai` scripted to return inflated
  usage; drive one turn via whichever of A/B is green; assert the **extra summarization request**
  appears in the capture JSONL (`SUMMARIZATION_PROMPT` for codex). Proves capability #4 paint-free.
- **Spike D — descending-ctx demo → negative `ctx_deltas`.** Script the stub to report *descending*
  `prompt_tokens` across ≥3 rounds; run `metrics_from_files`; assert `ctx_deltas` contains a
  negative. Proves "context gets smaller" deterministically, decoupled from real compaction.
- **Spike E — tool-call round-trip on the interactive path.** Stub emits one `ToolCall`
  (`loop_tool(harness)`); assert on the wire that the tool result flows back and the turn finishes.
  Optionally (codex/claurst only) `pyte`-render and grep the friendly marker (`Ran` / `Running
  command`).
- **Spike F — claurst chat-dialect wiring.** Confirm claurst dials `POST /v1/chat/completions` +
  `GET /api/tags` against `stubai` (NOT `/api/chat`), completes a turn, and its 99% path fires on
  inflated usage. Validates the correction in §2.
- **Spike G — ANSI-literal robustness.** For codex/claurst, compare raw-stream `grep` vs
  `pyte`-rendered-grid `grep` for a known marker; decide whether pyte is worth the dep or the wire
  alone suffices. (Hypothesis: wire alone suffices; pyte is a nice-to-have.)

- **Spike H — input injection (typing a prompt + submitting).** Once A/B is green, drive a real
  prompt in: `.await` the composer, write `"What is in this folder?"` then a bare `\r` (Option A) or
  `tmux send-keys -l "…" ; tmux send-keys Enter` (Option B), and assert the **first inference
  request appears on the wire**. Then repeat with the multi-line-ish factorial prompt to shake out
  bracketed-paste / Kitty-Enter encoding (§6.2). Start with **codex** (strict tty, clean markers).
  This spike de-risks the one uncertain part of the drive side; if bare `\r` fails to submit, test
  bracketed-paste wrappers and Kitty key-event encoding here.

Spikes A and C are the gating pair — A decides *how* we drive, C decides that the *hardest*
capability (compaction) is observable without paint. **H** immediately follows A (it proves we can
*send* work, not just start the process). Do A, then H, then C.

### 7.1 Empirical results — Spikes A + H + C RUN (codex, 2026-07-21) → **all green**

Driver: `scratchpad/spike_ac_codex.py` (host `pexpect` pty → `sbx exec -it … codex`, screen
rendered with `pyte`, assertions off the capture JSONL). danno built the exact launch argv/env via
`sandbox.launch`; the spike intercepted only the terminal handoff (`Runner.run` for the launch) and
drove that argv under its own pty. codex v0.144.5, model `stub` (stubai), egress `localhost:11455`
only. **Option A (host pty) is confirmed viable — lock it; Option B/tmux not needed.**

- **Spike A — PASS (real).** The pty nests cleanly through `sbx exec -it`: pyte rendered codex's
  full TUI (ASCII banner → composer box `>_ OpenAI Codex (v0.144.5)`, `model: stub`,
  `/model to change`). **No** "not a terminal" error. Two gotchas, both now findings:
  1. **Full env required.** For the no-secret codex path, danno's `env_forward_argv` returns
     `None` ("inherit caller env"); a pty child spawned with an *explicit empty* env makes the sbx
     docker plugin panic `$HOME is not defined` **before codex starts**. Fix: spawn with
     `{**os.environ, **(forwarded or {})}` (+ `TERM`). The plan's pty fixture must pass a full env.
  2. **First-run trust dialog blocks the composer.** codex parks on *"Do you trust the contents of
     this directory? 1. Yes, continue / 2. No, quit"* before the composer. The driver must answer
     it (`1`+Enter) — or the test should **pre-seed trust** in `$CODEX_HOME`/project config for an
     unattended run. The composer marker for THIS build is `/model to change` (the model line), **not**
     `Ask Codex to do anything` (that string never appears in v0.144.5; the placeholder is a rotating
     tip like *"Improve documentation in @filename"*). → update §3 marker + §7 Spike-A assert.
- **Spike H — PASS.** Typing `"What is in this folder?"` then a bare `\r` submitted the turn: the
  first `POST /responses` appeared on the wire and the stub reply (`Hello from the stub.`) painted.
  A second prompt (`\r`) submitted a second turn. **Bare `\r` submits — no bracketed-paste / Kitty
  wrapper needed** for a single-line prompt on codex.
- **Spike C — PASS (real, after two false starts).** With the stub reporting inflated usage
  (`total_tokens: 50005`) and `model_auto_compact_token_limit = 200`, codex compacted — pyte caught
  `• Context compacted` + the multi-compaction heads-up on screen, and the **wire shows the full
  server-side summarization**: 3 `POST /responses` for 2 user turns, where the extra request is a
  dedicated **`CONTEXT CHECKPOINT COMPACTION … create a handoff summary`** turn, and the next turn's
  history is **rewritten** with an *"Another language model … produced a summary of its thinking"*
  graft (old assistant turn dropped). So codex's config-knob auto-compaction **IS wire-visible** as a
  distinct summarization request — the two-modes ambiguity (§2) resolves to *server-summarization*
  for this path, not silent local truncation.
  - **Two detector traps to bake into the plan.** (a) The auto-compact key is **top-level** codex
    config — appended under `[model_providers.*]` it is silently ignored (compaction never fires).
    (b) A naïve `"summar"/"compact"/"concise"` substring match **false-positives on every request**
    (codex's static system prompt contains "summarize"/"concise"). Match codex's **actual** compaction
    phrasing (`context checkpoint compaction`, `create a handoff summary`, `produced a summary of its
    thinking`), scanning **all** input items (the graft lands mid-history; item *count* stays flat at
    `[3,5,5]`, so a count-shrink heuristic alone misses it).
- **stubai limitation confirmed.** Step classes carry no token override; inflating usage required
  monkeypatching `ScriptEngine.next_reply` → `prompt_tokens`. For the real test, either add a
  usage-override to the Step classes or keep a small fixture shim. (Reply already has the fields.)

Net: the lowered-bar capabilities are **all reachable and wire-observable on codex via a host pty** —
interactive TUI reached, AI connected, turn dispatched, and compaction fired + captured. Spikes B, D,
E, F, G remain (tmux fallback now optional; claurst/opencode still need their own A/H/C confirmation).

### 7.2 Empirical results — Spikes A + H + C RUN (opencode, 2026-07-21) → **all green**

Driver: `scratchpad/spike_harness.py opencode` (same host-`pexpect`→`sbx exec -it`→`pyte` rig,
generalized; per-harness config dict). opencode v1.17.11, model `stub`, egress `localhost:11455` only.
Launch argv is the bare `opencode` binary (model + capture come from the seeded `opencode.jsonc`;
danno forwards `OLLAMA_BASE_URL` by name via `-e`).

- **Spike A — PASS.** pty nests; pyte rendered the full opencode TUI (ASCII banner → composer box
  `┃ Ask anything…` / `Build · stub (local) ollama (local)`). No "not a terminal" error.
- **Spike H — PASS.** Typing a prompt + bare `\r` submitted the turn; `POST /chat/completions`
  appeared and the stub reply (`Hello from the stub.`) painted. Footer showed `50.0K (156%)` — the
  inflated usage registered client-side.
- **Spike C — PASS (bounded, after two fixes).** With one-shot inflated usage, opencode fired
  **exactly one** compaction: pyte caught the ` Compaction ` divider + a `Compaction · stub` step,
  and the footer reset **`156%` → `0%`** (context rebuilt). Wire: `requests=5,
  item_counts=[2,4,4,6,8], summarization_requests=1` — the summarization request carries opencode's
  distinctive `create a new anchored summary from the conversation history` / `output exactly the
  Markdown structure`. So opencode compaction **IS wire-visible** as a dedicated anchored-summary
  request.
- **Two blocking gotchas — both now findings the plan MUST encode:**
  1. **Auto-update modal, appearing LATE, must be ESC'd — never Enter'd.** opencode pops an
     *"Update Available"* modal **a couple seconds AFTER** the composer is already up (it does **not**
     always appear). Its footer hint is `esc` = Skip. **If Enter reaches it, opencode CONFIRMS the
     update**, downloads + installs **v1.18.4**, and then requires a restart — the process **exits**
     (pty EOF), mutating the harness-under-test mid-run and killing the session. Fix: after reaching
     the composer, spend a fixed **settle window watching for the (late) modal and send ESC**, and
     **never send Enter while a modal could be up**. With ESC, opencode stayed pristine at **1.17.11**.
     *(Production test should also seed `autoupdate:false` — belt to the ESC braces. And note: the
     updater reached the internet and pulled a binary **despite egress allowing only
     `localhost:11455`** — worth a separate look; the update path may bypass the sandbox egress.)*
  2. **One-shot usage inflation is mandatory — else a compaction runaway.** If *every* stub reply
     reports 50k tokens, usage never falls below the threshold and opencode auto-compacts in an
     **unbounded loop** (observed **1184 requests / 592 summarizations in ~67 s**). Inflate only the
     **first** reply (cross the line once, then report normal usage) → exactly one compaction. This is
     the same runaway class the bench "runaway gates" guard against — the test's stub must bound it.

### 7.3 Empirical results — Spikes A + H + C RUN (claurst, 2026-07-21) → **A/H green; C decisively NOT armed**

Driver: `scratchpad/spike_harness.py claurst`. Fork build **`v0.1.6-danno1`** (danno installs it into
the `shell` VM via `install_claurst`), model `ollama/stub`, egress `localhost:11455` only. Launch argv
is `bash -lc "OLLAMA_HOST=…proxy… claurst -m ollama/stub"`.

- **Spike A — PASS.** pty nests; pyte rendered claurst's `╭ Claurst v0.1.6 ─╮` welcome panel + the
  composer. **Composer glyph is `❯` (U+276F), NOT ascii `>`** — a `>` marker false-FAILS A2 even though
  the composer is present. → plan's claurst composer marker must be `❯`.
- **Spike H — PASS (needs a robust submit).** A real turn reached the wire (`POST /chat/completions`)
  and the stub reply (`Hello from the stub.`) painted. **Two input gotchas:**
  1. **First-run "Keyboard Shortcuts 2/2" onboarding overlay** appears over the composer, **time-
     triggered and racing the typing** (sometimes before prompt-1, sometimes after) — footer
     `enter done · esc close`; **ESC** closes it. Because the `❯` prompt is visible *under* the
     overlay, "composer present" ≠ "ready".
  2. **`?` opens claurst's help overlay** — a prompt containing `?` (e.g. `"What is in this folder?"`)
     pops the shortcuts overlay and eats the following prompts. Use `?`-free prompt text.
  → The reusable primitive that made H reliable: **`submit()` = dismiss modals → type + Enter →
    CONFIRM a new request landed on the wire → retry (dismiss again) if not.** A racing overlay can
    silently eat the Enter; only a wire-count check catches it. The plan's claurst driver needs this.
- **Spike C — NOT OBSERVED (decisive, not a tuning miss).** claurst did **not** auto-compact.
  Ruled out both usual suspects on the wire:
  - **Usage DOES flow** (the frozen-meter memory is *not* the cause here): claurst requests
    `stream_options:{include_usage:true}` on every turn, and the stub returned `total_tokens` of
    **200005**, then — pushing harder — **2000005**. Confirmed received on turn 1.
  - **Even at 2,000,000 reported tokens, no compaction**: `requests=3, item_counts=[2,4,6]` (history
    *grows*, never shrinks), `summarization_requests=0`, no summary step painted. So it is **not** a
    window-belief/threshold problem (2M exceeds any window) and **not** a frozen meter (usage arrived).
  - **Conclusion:** interactive claurst `v0.1.6-danno1` (default config, ollama provider) **does not
    arm usage-driven auto-compaction** — consistent with the danno "auto-compact-safety" fix that
    deliberately tamed the upstream never-prunes runaway. **Implication for the plan:** claurst's
    compaction leg is **not assertable via usage inflation** the way codex/opencode are; either drop
    claurst's C assertion, or reach compaction another way (e.g. ship `CLAURST_MODELS_PATH` for a
    correct/small window + whatever config re-arms auto-compact, or trigger the interactive 99% path —
    both unverified). A/H (interactive TUI + AI connected + real turn on wire) are solid for claurst.

Net across all three: **A + H (interactive entry + AI connected + turn on wire) PASS on codex,
opencode, claurst.** Compaction-on-wire (C) is proven on **codex** (config-knob) and **opencode**
(one-shot inflate, bounded), and is **not reachable via usage inflation on claurst** in the current
build/config. The host-pty drive (Option A) + `submit()`-with-wire-confirm + settle-then-ESC-modals is
the reusable harness shape for the eventual `@slow` test. Spikes B, D, E, F, G remain optional.

---

## 8. Proposed acceptance shape (informing the eventual plan — NOT the plan)

One `@slow` test (or a small parametrized family over `{opencode, codex, claurst}`) that, per
harness:

1. provisions a sandbox with egress allowing ONLY the stub host (fail-loud);
2. drives `danno sandbox start --harness <h>` through a **pty** (Option A, else tmux) so the
   **interactive** entry path runs — asserted by a startup marker on the wire and/or paint
   (`Ask Codex to do anything` / ` Claurst ` / opencode: first `/v1/...` request observed);
3. **types a real prompt into the composer** (e.g. *"What is in this folder?"*, then the factorial
   *"write a python program that prints 100! and run it"*) via the pty / `tmux send-keys` (§6.2),
   asserting the turn dispatches on the wire — with `stubai` scripted for: a **tool-call** round
   (mirroring the list/read), a **write→run tool loop** (mirroring the factorial), then
   **descending** `prompt_tokens`, then an **inflated-usage** round;
4. asserts, **primarily off the capture JSONL + `wire_metrics`**:
   - AI connected (≥1 completed inference request/response),
   - tool call executed (tool-result round-trips on the wire),
   - context shrinks (`ctx_deltas` has a negative),
   - compaction fired (distinct summarization request present),
   - danno saw it (`render_transcript` non-empty / metrics populated);
5. tears down; skips cleanly when `sandbox_runtime_down()`.

Real-AI leg (local Ollama qwen) is a **second, thinner** variant proving the same interactive path
against a real model for at least one harness — added only after the stub variant is green.

### 8.1 Compaction is a per-harness CAPABILITY, and claurst's leg is a change-detector (DECIDED 2026-07-21)

Given §7.3 (claurst does not compact on usage even at 2M), compaction becomes an explicit harness
capability rather than a universal assertion:

- **Flag lives on the `Harness` value object** (`harnesses/__init__.py`, the optional-defaulted
  block): add `compacts: bool = True` next to `capture_via_relay: bool = False`. codex + opencode take
  the default `True`; **claurst overrides to `False`**. This matches the existing capability-boundary
  pattern (`speaks`, `dials`, `supports_capture`) — the test branches on the flag, not the name.
- **The C leg branches on `harness.compacts`:**
  - `compacts=True` → assert compaction **fired**: `summarization_requests ≥ 1` (+ history shrink /
    ` Compaction ` divider / `Context compacted`).
  - `compacts=False` (claurst) → assert compaction **did NOT fire**: `summarization_requests == 0`.
- **The negative assert needs no extra precondition ceremony.** claurst simply cannot auto-compact, so
  `== 0` is a stable fact; the only thing that flips it to ≥1 is claurst *gaining* the capability. A
  broken turn flow never manufactures a false compaction — it just fails the same test's **H** assert
  (a turn dispatched on the wire), which every harness already runs. So H covers "turns happened," and
  the C leg is a bare `== 0`. (The stub still feeds the inflated-usage round to claurst — same script
  as the compacting harnesses — so the day claurst learns to compact, it will, and the assert breaks.)
- **Change-detector semantics (the user's intent):** the day claurst starts compacting, the
  `summarization_requests == 0` assert goes **red — loud**. That is the signal to make a **conscious**
  release decision: flip claurst's flag to `compacts=True`, add a changelog line ("claurst now
  compacts as of build X"), and ship a new danno version whose test-expectation matches. The test
  never silently absorbs the behavior change — it forces the human flip. (Same fail-loud spirit as the
  sandbox-security contract: encode reality, break loudly when reality moves.)

---

## 9. Open items / carry-forward

- **Drive fork (Option A vs B)** — **RESOLVED 2026-07-21 (Spike A): Option A (host pty) works** for
  codex through `sbx exec -it`; lock it. Two must-dos for the fixture: pass a **full env**
  (`{**os.environ, …}`, or the sbx plugin panics `$HOME is not defined`) and **handle/pre-seed the
  trust dialog**. Option B (tmux) demoted to fallback-only, needed only if opencode/claurst prove
  flaky under host pty.
- **tmux-in-VM (A3)** — still unverified; Spike B (now only matters if a harness fails under Option A).
- **Issue #112 (eager capture file)** — not required for this test, but lazy-create would make
  "was this backend dialed" a clean file-existence signal for the assertion.
- **opencode paint is not grep-able** — accept wire-only assertions for opencode; don't fight the
  opentui grid.
- **codex `TERM=dumb`** — ensure the pty advertises a real TERM (e.g. `xterm-256color`), or codex
  refuses.
- Plan file to be written after the user evaluates this doc:
  `/Users/mikestitt/.claude/plans/recursive-hopping-snowflake.md`.
