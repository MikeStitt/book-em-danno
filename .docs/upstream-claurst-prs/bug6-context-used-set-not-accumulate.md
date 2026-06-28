# Bug 6 — Context meter "used" accumulates across turns and over-counts

Branch: `fix/context-used-set-not-accumulate` (commit `5e2a4a2`, off `upstream/main` 59c397f)
File: `src-rust/crates/tui/src/app.rs`

---

## Bug report (→ GitHub issue)

**What happens**

On each completed turn, the TUI adds the turn's full usage to a running total:

```rust
let turn_tokens = u.input_tokens + u.output_tokens
    + u.cache_creation_input_tokens + u.cache_read_input_tokens;
self.context_used_tokens = self.context_used_tokens.saturating_add(turn_tokens);
```

But each turn's `input_tokens` (plus cached) **already covers the entire conversation so
far** — the prompt is the whole running history. Accumulating it every turn therefore sums
the growing history repeatedly, so the meter's "used" count grows far faster than the real
context occupancy and quickly overstates it (often exceeding the window after only a few
turns).

**Expected:** "used" should reflect the *current* context occupancy = the latest turn's
prompt (input + cached) plus its output.

---

## PR

**Title:** fix(tui): set context_used_tokens to latest turn, don't accumulate

**Summary**

Replace the saturating-add with a plain assignment: the latest turn's
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens`
**is** the current context occupancy, so set it rather than summing across turns.

(Pairs with Bug 5: Bug 5 makes the usage number arrive for Ollama/NIM; Bug 6 makes it
applied correctly.)

**Diff**

```diff
diff --git a/src-rust/crates/tui/src/app.rs b/src-rust/crates/tui/src/app.rs
@@ -5978,11 +5978,16 @@ impl App {
                 self.is_streaming = false;
                 self.spinner_verb = None;

-                // Update context window usage from the usage info.
+                // Update context window usage from the usage info. The prompt
+                // (input_tokens + cached) already covers the entire conversation
+                // so far, so the latest turn's prompt + its output *is* the
+                // current context occupancy — set it, don't accumulate across
+                // turns (that would sum the growing history every turn).
                 if let Some(ref u) = usage {
-                    let turn_tokens = u.input_tokens + u.output_tokens
-                        + u.cache_creation_input_tokens + u.cache_read_input_tokens;
-                    self.context_used_tokens = self.context_used_tokens.saturating_add(turn_tokens);
+                    self.context_used_tokens = u.input_tokens
+                        + u.cache_creation_input_tokens
+                        + u.cache_read_input_tokens
+                        + u.output_tokens;
                 }
                 // Record elapsed time and pick a completion verb
                 let seed = self.frame_count as usize ^ (self.messages.len() * 7);
```

**Testing**
- Multi-turn session: "used" tracks the real prompt size each turn instead of ballooning;
  verified against Ollama where the prompt growth is observable per turn.
