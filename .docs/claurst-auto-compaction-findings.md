# claurst auto-compaction — findings (the real bug vs. enhancements)

_Analysis captured 2026-07-12 while scoping "Bug 10" (make auto-compact actually
prune). **For review.** All line numbers are against the danno fork base
`upstream/main` = `59c397f` unless noted. Companion to
`.docs/upstream-claurst-prs/` (Bug 8/9 drafts) and the memory
`claurst-auto-compact-armed`._

## TL;DR (the crux)

claurst has **four** context-compaction surfaces, but only **one** actually
prunes history — and that one is misconfigured for local models, so on Ollama /
NVIDIA it never fires. The compaction a user actually hits interactively
(auto-compact at 99% and the `/compact` command) does **not** prune: it asks the
model for a summary, appends it, and (for the auto path) fakes the token counter
— which is the livelock behind Bug 8. So:

- **The bug:** interactive/TUI auto-compact and `/compact` never free context.
  Bug 8 makes the auto path *safe* (stops the livelock); the remaining defect
  ("it doesn't actually compact") is the Bug 10 bug.
- **Not reviving dead code:** the real pruning function
  (`compact::compact_conversation`) is **live** via a different path
  (`run_query_loop`), so "Bug 10" = point the broken surfaces at the function
  that already works, not resurrect something dead.
- **The deeper issue is architectural:** two independent auto-compact systems
  with *different* trigger thresholds and *different* context-window sources, so
  on local models the wrong one fires.

## Naming clarification — why "anthropic" is on the compactor

`compact_conversation(client: &AnthropicClient, …)` is **not** Anthropic-only.
claurst began as a clean-room reimplementation of Claude Code, so its universal
types kept Anthropic names:

- **`AnthropicClient`** (`api/src/lib.rs:457`, ~44 refs) — the *universal* client.
  It routes by `config.provider`; Ollama and NVIDIA NIM run through it via the
  `openai_compat` adapters. Not provider-specific.
- **`AnthropicStreamEvent`** (~83 refs) — the *canonical internal* event shape
  every provider's stream is normalized *into* (`map_to_anthropic_event`,
  `anthropic_to_openai_request`). Also universal.
- **`AnthropicProvider`** (~13 refs) — the genuinely Anthropic-specific adapter,
  a sibling of `GoogleProvider` and the `openai_compat` providers.

So a function taking `&AnthropicClient` is provider-agnostic; the name is a
clean-room artifact, not a constraint.

## The four compaction surfaces

| # | Surface | Prunes? | Where | Trigger / window source |
|---|---------|---------|-------|--------------------------|
| A | `auto_compact_if_needed` → `compact_conversation` | **YES** (keeps last 10, real summary, replaces head) | `query/src/lib.rs:1708` (default branch); fn at `compact.rs:652`/`683` | 90% of `context_window_for_model()` (`compact.rs:489`, **hardcoded**), on real `usage.input_tokens` |
| A′ | `reactive_compact` / `context_collapse` | **YES** | `query/src/lib.rs:1652`,`1682` | **feature-gated off**: `CLAURST_FEATURE_REACTIVE_COMPACT` (`lib.rs:1646`) |
| B | interactive auto-compact | **NO** (injects summary req, appends, fakes counter → livelock) | `cli/src/main.rs:2995`–`3053`; fake reset `main.rs:3620`–`3625` | 99% of `app.context_window_size` / `app.context_used_tokens` (`tui/app.rs`), window from registry else fallback (`refresh_context_window_size` `app.rs:1870`; Bug 4 = 8192 local) |
| C | manual `/compact` | **NO** (emits a `UserMessage` summary request, appended) | `commands/src/lib.rs:619`; TUI defers to CLI loop `tui/app.rs:2093` | user-invoked |

Dead helpers (imported, no call sites): `micro_compact_if_needed`,
`snip_compact` (`query/src/lib.rs:34`–`35`).

## Where each surface runs — TUI vs. non-TUI

`run_query_loop` (System A/A′) is reached by **every** entry path:
`run_interactive` (`main.rs:1607`, turns at `2711/3037/3189/3297/3738`),
`run_headless` / `-p` (`main.rs:1309`→`1411`), ACP (`acp/src/prompt.rs:90`),
sub-agents (`agent_tool.rs:399/465/635`), and cron (`cron_scheduler.rs:85`).
System B lives in the TUI event loop and mutates `app`, so it is **TUI-only**.

| Surface | Interactive TUI | Headless `-p` | ACP | Sub-agent / team | Cron |
|---------|:---:|:---:|:---:|:---:|:---:|
| A (prunes) | ✅ | ✅ | ✅ | ✅ | ✅ |
| A′ (prunes, gated off) | ✅* | ✅* | ✅* | ✅* | ✅* |
| B (no prune, livelock) | ✅ | — | — | — | — |
| C `/compact` (no prune) | ✅ | — (no slash cmds) | — | — | — |

\* only when `CLAURST_FEATURE_REACTIVE_COMPACT=1`.

## Which AI paths each surface affects

Two different window sources diverge by provider:

- **System A — `context_window_for_model()` (`compact.rs:489`):**
  `opus-4|sonnet-4|haiku-4|claude-3.5 → 200_000`, **everything else → 100_000**.
  - *Claude 4 / 3.5 (cloud):* ~correct (200K) → System A can fire and prune.
  - *Local Ollama / NVIDIA NIM:* falls to 100K. A 64K-window model can never
    reach 90K `input_tokens`, so **System A never fires** for local models.
    (Also: Ollama `usage.input_tokens` only advances after **Bug 5**; without it,
    frozen at 0 → never fires regardless.)
  - *Other cloud (OpenAI, Gemini):* also 100K guess — wrong for Gemini's ~1M
    (compacts far too early) and for GPT variants.
- **System B — `app.context_window_size` (`refresh_context_window_size`):**
  registry-backed when danno injects `CLAURST_MODELS_PATH`, else a fallback
  (anthropic 200K / openai 128K / google 1M / **local 8192** via Bug 4 / other
  128K), flagged `is_estimate` by Bug 8. Fires at 99%.
  - *Local models:* fires at ~99% of 8192 → this is the surface that livelocks
    for Ollama/NVIDIA — i.e. **the only auto-compaction that fires for local
    models is the broken one**, because System A is inert for them.
  - *Claude cloud:* fires at 99% of 200K; its injected turn can *incidentally*
    trigger System A at end-of-turn (input near 90% of 200K), so on Claude the
    real pruner sometimes runs anyway — masking the defect on cloud.

## Classification: bug vs. feature enhancement

### BUG (crux — should be fixed)
Interactive auto-compact (B) and `/compact` (C) **do not free context**: they
append a summary and never prune, and B additionally fakes the counter reset →
re-fires forever (livelock). This is user-visible only where B/C run — **TUI /
interactive**, and worst on **local models** (Ollama/NVIDIA), where System A is
inert so nothing else compacts. On **Claude cloud**, System A sometimes prunes
incidentally, partly masking it. Bug 8 already lands the *safety* half (latch +
provenance + honest status); the remaining "actually prune" half is **Bug 10**.

### FEATURE ENHANCEMENTS (improvements, not defects)
- **E1 — route B and C through `compact_conversation`** so interactive
  compaction actually prunes (reuses the live System A function). _TUI/interactive;
  all AI paths (biggest win on local models)._ This is the minimal Bug 10 fix.
- **E2 — fix System A's window source:** use the registry / real `num_ctx`
  (via `CLAURST_MODELS_PATH`) instead of the hardcoded `context_window_for_model`,
  so System A fires correctly for local models — after which System B could be
  retired. _All paths (TUI + headless + ACP + sub-agent + cron); primarily local
  + Gemini AI paths._
- **E3 — unify the surfaces** onto one compaction service (A, B, C share one
  implementation and one window source). _All paths; architectural._
- **E4 — resolve the dead helpers** (`micro_compact_if_needed`, `snip_compact`):
  wire or delete. _N/A to runtime; hygiene._
- **E5 — decide `reactive_compact`'s fate:** it's a complete, feature-gated-off
  alternative pruner. Promote, or remove to cut confusion. _All paths (when gated
  on); currently no AI path, since off by default._

## Open questions for the reviewer
1. **Scope of "Bug 10":** minimal **E1** (make B/C prune via the live function),
   or take **E2** (fix the window so System A works and retire B) — the latter is
   the more honest fix but a larger change touching a universal path.
2. **Double-compaction risk on cloud:** if B prunes *and* System A can also fire
   at end-of-turn, do we need a guard so a single turn doesn't compact twice?
   (Not an issue on local, where A is inert.)
3. **Is B worth keeping at all** if E2 makes A fire correctly for every provider?
   B is TUI-only and duplicates A with a worse window source.
4. **`reactive_compact` (A′):** in or out? Leaving a gated-off second pruner is
   part of why this area is confusing.
