# Bug 8 — Auto-compact fires unprompted turns it can never satisfy (livelock), including on guessed context windows

Branch: `fix/auto-compact-safety` (commit `9be67e1`, off `upstream/main` 59c397f) —
**the required base fix for the context-meter cluster** (Bugs 4/5/10; see README).
Code written 2026-07-12; compiles (`cargo check -p claurst`). Behavioral re-verification
against a live session still pending.
Files: `src-rust/crates/cli/src/main.rs` (trigger ~2995 at fork point `59c397f`,
~3236 at current upstream tip), `src-rust/crates/tui/src/app.rs` (window fallback).

---

## Bug report (→ GitHub issue)

**What happens**

The CLI event loop auto-dispatches a "compact" turn whenever the context meter reads
≥ 99%:

```rust
// cli/src/main.rs (~2995 @ 59c397f)
if app.context_window_size > 0
    && !app.is_streaming
    && current_query.is_none()
    && !app.auto_compact_running
{
    let used_pct = (app.context_used_tokens as f64 / app.context_window_size as f64 * 100.0) as u64;
    if used_pct >= 99 {
        // pushes "[Auto-compact triggered …]" as a *user* message and
        // spawns run_query_loop — a turn the user never typed.
```

Three defects compound:

1. **It livelocks — the "compact" never compacts.** The injected turn only *appends* the
   model's summary to `messages`; nothing is pruned or replaced. `context_used_tokens` is
   zeroed when the compact turn completes (`main.rs` TurnComplete handler), but the very
   next real turn's `usage` sets it straight back over the threshold — so auto-compact
   re-fires after **every** completed turn, forever. On a local model each cycle is a full
   prompt prefill (minutes). Observed live 2026-07-10 (danno fork, Ollama
   `qwen3-coder-next` 64 K): after context passed the (mis-measured, see 2) threshold,
   every user turn was followed by an unprompted compact turn.

2. **It fires on *guessed* denominators.** When the model misses the models.dev registry,
   `context_window_size` is a provider-keyed fallback guess (`app.rs`
   `refresh_context_window_size()` — 128 K catch-all upstream; 8 192 for local providers
   with the Bug 4 patch). Auto-dispatching turns the user never asked for, based on an
   *estimate*, is wrong regardless of the estimate's value — with a small conservative
   guess it fires almost immediately; with a large guess it fires arbitrarily late or on
   models whose real window is bigger than the guess.

3. **The user can't stop it.** ESC doesn't cancel the running query (Bug 9), and even
   after a successful Ctrl+C cancel the ≥ 99% condition simply re-fires on the next tick.

**Who is affected upstream today:** any provider whose streamed `usage` flows (Anthropic,
OpenAI, …). The over-counting accumulation (Bug 6) makes the meter cross 99% *early* in
long sessions, at which point the livelock begins. Ollama/NVIDIA users are only exempt
because usage never flows for them (Bug 5) — i.e. one display bug is currently masking a
control-flow bug. Fixing Bug 5 (or 4) without this fix arms the livelock for local
models; that is exactly how it was discovered.

**Expected**

- Auto-compact only ever fires against a **registry-backed** window, never a fallback
  guess.
- Auto-compact is **latched**: after firing once, it does not re-fire until usage has
  actually dropped back below a re-arm threshold (hysteresis, e.g. < 95%). If the compact
  turn did not reduce usage, say so loudly in the status line instead of retrying forever.
- Ideally, the compact turn actually **replaces** the summarized history instead of
  appending to it (that is what would make re-arming meaningful).

---

## Fix direction (→ PR, to be written)

1. Add window provenance — e.g. `context_window_is_estimate: bool` set in the fallback
   arm of `refresh_context_window_size()` — and require `!context_window_is_estimate` in
   the auto-compact condition. (Touches the same `match provider` arm as Bug 4, which is
   why Bug 4 must stack on this branch.)
2. Add a re-arm latch: a `auto_compact_latched: bool` set when a compact turn completes
   with `used_pct` still ≥ the trigger threshold; cleared only when `used_pct` drops below
   a re-arm threshold. Surface "auto-compact did not reduce context usage" as a status
   notification when latching.
3. Making the compact turn actually prune history (replace the summarized prefix instead
   of appending) is **split out as [Bug 10](bug10-auto-compact-prune-history.md)** to keep
   this base PR small — Bug 8 makes auto-compact *safe*, Bug 10 makes it *effective*.

**As implemented on `fix/auto-compact-safety`:** added `App.context_window_is_estimate`
(set in both arms of `refresh_context_window_size`) and `App.auto_compact_latched`; gated
the `main.rs` trigger on `!context_window_is_estimate && !auto_compact_latched`; replaced the
cosmetic `context_used_tokens = 0` reset in the turn-complete handler with an honest
used-pct check that latches + surfaces a loud status when compact didn't help; the latch
clears on hysteresis (usage < 95%) or a genuine context reset (model switch / config import).

**Testing**
- With a registry-backed window: drive usage to 99%, observe exactly **one** compact
  turn, then the latch notification — no second unprompted request (assertable on the
  wire against a stub server).
- With a fallback-guessed window: no auto-compact ever fires.
- Regression: Bug 5 + Bug 4 applied on top, local Ollama model absent from the registry,
  long agentic session — zero unprompted turns.
