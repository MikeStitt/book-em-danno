# Bug 10 — Auto-compact appends a summary but never prunes history, so it cannot reduce context

Branch: _none yet_ — issue-first: mechanism understood, fix direction below, code not yet
written. **Depends on Bug 8** (`fix/auto-compact-safety`): split out of that fix to keep the
base PR small. Bug 8 makes auto-compact *safe* (no livelock); Bug 10 makes it *effective*.
Files: `src-rust/crates/cli/src/main.rs` (auto-compact dispatch + turn-complete handler),
`src-rust/crates/tui/src/app.rs` (message list / context accounting).

---

## Bug report (→ GitHub issue)

**What happens**

When the context meter hits the auto-compact threshold, the CLI injects a "compact" user
turn that asks the model to summarize the conversation. But the turn only **appends** the
model's summary to `messages` — nothing in the prior history is pruned or replaced. The
turn-complete handler used to paper over this by zeroing `context_used_tokens` (a cosmetic
reset that Bug 8 removed), so the meter *looked* like it dropped while the underlying
message list kept growing. The very next real turn re-sends the entire (now larger) history
to the provider, and usage climbs straight back to the threshold.

Consequence: **auto-compact never actually frees context.** With Bug 8's re-arm latch in
place this no longer livelocks — after one ineffective compact turn the latch trips and the
status line says "Auto-compact did not reduce context usage; auto-compaction paused" — but
the user is left in a genuinely-full context with no automatic remedy. A real `/compact`
should shrink the working set; today neither the manual nor the automatic path does.

**Expected**

- The compact turn **replaces** the summarized prefix of `messages` with the single summary
  message (plus whatever recent tail should be preserved verbatim), so the next turn's
  prompt is materially smaller.
- After a successful compact, real post-compact `usage` reflects the smaller window, so
  Bug 8's re-arm latch clears naturally (usage < re-arm threshold) and auto-compact can help
  again later in a long session.

---

## Fix direction (→ PR, to be written)

1. After the compact turn completes, splice `messages`: drop the summarized prefix and
   insert the summary as a synthetic assistant/user message, preserving a configurable tail
   of the most recent turns verbatim (tool results the model may still need). Mirror this in
   `session.messages` and the persisted JSONL so reload is consistent.
2. Recompute `context_used_tokens` from the pruned message set rather than trusting the
   compact turn's own `usage` (which reflects the pre-prune prompt). This is what lets Bug
   8's hysteresis re-arm the latch honestly.
3. Wire the same prune into the manual `/compact` command so both paths share one
   implementation.

**Interaction with Bug 8**
- Bug 8 deliberately stops at "make it safe": provenance-gate + re-arm latch + honest status,
  and it *removed* the cosmetic `context_used_tokens = 0` reset so the meter tells the truth.
- Bug 10 is what makes the latch's re-arm path reachable in practice — without pruning,
  usage never drops below the re-arm threshold except on a genuine context reset.

**Testing**
- Drive a session to the threshold, trigger compact, assert `messages.len()` shrank and the
  next turn's on-the-wire prompt is smaller (stub provider).
- Assert real post-compact usage is below the re-arm threshold and Bug 8's latch is clear,
  so a later climb re-triggers exactly one more compact.
- Reload the persisted session and confirm the pruned history round-trips.
