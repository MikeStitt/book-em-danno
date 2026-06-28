# Bug 7 — Agent silently ships ZERO tools when a model's registry capability omits `tool_call`

Branch: `fix/warn-on-zero-tools` (commits `f07c81c`, `4e4c9c5`, off `upstream/main` 59c397f)
File: `src-rust/crates/query/src/lib.rs`

---

## Bug report (→ GitHub issue)

**What happens**

In `run_query_loop`, the tools actually sent to the provider are gated by the model's
registry capability (`tool_calling`). When a models catalog/overlay entry **omits the
`tool_call` field**, it defaults to `false`, so the agent ships **zero tools** — silently.

The model then "narrates" tool calls as plain text (it was never given any tools to call),
and the loop ends after a single turn. There is no error and no warning, so the failure is
confusing and hard to diagnose: it looks like the model is just refusing to act.

This is especially easy to hit with custom/overlay model registries (e.g. a generated
catalog entry that forgot `tool_call = true`).

**Expected:** a loud, visible warning when 0 tools are sent because the capability reports
`tool_calling = false`, *especially in headless `-p` runs* where no WARN-level tracing is
printed.

---

## PR

**Title:** fix(query): warn when a model is sent zero tools due to registry capability

**Summary**

When `provider_tools` is empty but tools *are* available (`!tools.is_empty()`), emit a
warning explaining that the model's registry capability reports `tool_calling = false`, so
0 of N tools were sent and the run will narrate-then-stop. The message names the
provider/model and tells the user to set `tool_call = true` in the registry entry.

Uses `eprintln!` (not `tracing::warn!`) deliberately: headless `-p` runs install no tracing
subscriber, so a `warn!` would be invisible — which is the exact scenario where this silent
failure bites.

**Diff**

```diff
diff --git a/src-rust/crates/query/src/lib.rs b/src-rust/crates/query/src/lib.rs
@@ -1108,6 +1108,25 @@ pub async fn run_query_loop(
                     } else {
                         Vec::new()
                     };
+                    // Fail loud: when a model's registry capability reports tool_calling=false
+                    // (e.g. a models catalog/overlay entry that omits the `tool_call` field, which
+                    // then defaults to false), the agent silently ships ZERO tools. The model then
+                    // narrates tool calls as plain text and the loop ends after one turn — a
+                    // confusing, hard-to-diagnose failure. Surface it instead of hiding it.
+                    if provider_tools.is_empty() && !tools.is_empty() {
+                        // eprintln! (not tracing::warn!) so this is visible in headless `-p` runs,
+                        // where no tracing subscriber prints WARN-level events — otherwise the
+                        // failure stays silent, which is the whole problem we're surfacing.
+                        eprintln!(
+                            "[claurst] tools disabled for {}/{}: its model-registry capability \
+                             reports tool_calling=false, so 0 of {} available tools were sent. The \
+                             model will narrate tool calls as text and the run will stop after one \
+                             turn. If it can tool-call, set tool_call=true in its model-registry entry.",
+                            provider_id_str,
+                            model_id_str,
+                            tools.len()
+                        );
+                    }
                     let provider_messages: Vec<claurst_core::types::Message> = messages
                         .iter()
                         .map(|msg| {
```

**Testing**
- A catalog entry missing `tool_call` now prints the `[claurst] tools disabled for …`
  warning to stderr in a headless `-p` run (was silent); fixing the entry to
  `tool_call = true` clears it and tools are sent.
