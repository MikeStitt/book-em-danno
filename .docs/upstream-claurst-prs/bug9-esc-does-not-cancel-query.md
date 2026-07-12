# Bug 9 — ESC doesn't cancel the running query; after ESC, Ctrl+C can't either, and Enter double-spawns

Branch: `fix/esc-cancels-query` (commit `1c1ad5d`, off `upstream/main` 59c397f). Code
written 2026-07-12; compiles (`cargo check -p claurst`). Behavioral re-verification against
a live session still pending. Independent of the context-meter cluster, but the second half
of the "unprompted turns you can't stop" failure (see Bug 8).
Files: `src-rust/crates/tui/src/app.rs` (ESC handler ~3979),
`src-rust/crates/cli/src/main.rs` (`handle_exit_key` ~343, submit path ~2658).

**As implemented:** intercept `KeyCode::Esc` in the `main.rs` event loop and `ct.cancel()`
when `current_query.is_some()` before falling through to the TUI handler; change
`handle_exit_key` to take a `query_active: bool` and gate its Ctrl+C cancel on that instead
of `app.is_streaming`; branch the Enter queue-vs-submit decision on `current_query` rather
than `app.is_streaming` so a live query can't be double-spawned.

---

## Bug report (→ GitHub issue)

**What happens**

The status bar advertises "esc interrupt", but ESC only mutates TUI state:

```rust
// tui/src/app.rs:3979
KeyCode::Esc if self.is_streaming => {
    self.is_streaming = false;
    // clears spinner/stream buffers, prints "Cancelled."
    // …the CancellationToken is never cancelled.
}
```

The CLI event loop never intercepts Esc (zero `KeyCode::Esc` matches in `main.rs`), so
the spawned `run_query_loop` task keeps streaming from the provider and **keeps executing
tools** while the UI claims "Cancelled."

Two compounding effects, both verified by code inspection:

1. **After ESC, Ctrl+C can't cancel either.** `handle_exit_key` (`main.rs:343`) gates the
   token cancel on `app.is_streaming` — which ESC just set to `false`. Ctrl+C therefore
   skips `ct.cancel()` and falls through to the exit-confirmation flow. The only ways to
   stop the running query after an ESC are quitting the app or the remote-bridge cancel.
2. **After ESC, Enter double-spawns.** The submit path (`main.rs` ~2658) picks
   "queue vs. submit" by `app.is_streaming` and has no `current_query.is_some()` guard.
   With `is_streaming` falsely `false`, a new message spawns a **second concurrent**
   `run_query_loop` and overwrites `current_query`/`cancel` — the first task is leaked
   (still running tools, still emitting `QueryEvent`s into the same channel), and two
   agent loops interleave into one transcript. From the user's seat: the agent acts
   without direction.

**Expected:** ESC cancels the in-flight query (the token exists precisely for that), a
cancelled/orphaned task can always be reached by Ctrl+C, and a new submission can never
race a live query.

---

## Fix direction (→ PR, to be written)

1. Intercept Esc in the CLI key handling (next to `handle_exit_key`): if
   `current_query.is_some()`, `ct.cancel()` **then** let the TUI handler do its state
   cleanup.
2. In `handle_exit_key`, gate the Ctrl+C cancel on `current_query.is_some()` (or
   `cancel.is_some()`), not `app.is_streaming` — the UI flag is not ground truth for
   "work is running".
3. In the submit path, branch on `current_query.is_some()` rather than
   `app.is_streaming` when deciding queue-vs-submit, so a live query can never be
   double-spawned. (The queued-messages path already exists; this just picks it
   reliably.)

**Testing**
- Wire-level (stub provider): ESC mid-stream → the in-flight request's connection drops
  and no further request arrives; a queued Enter submits only after the cancel completes.
- ESC then Ctrl+C → token cancelled (no exit-confirmation hijack while work is running).
- ESC then Enter → exactly one `run_query_loop` alive at any time (no interleaved
  transcripts).
