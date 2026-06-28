# Bug 4 — Context-window fallback assumes 128 K for local providers, badly over-reporting the meter

Branch: `fix/ollama-context-window-fallback` (commit `edfc2bd`, off `upstream/main` 59c397f)
File: `src-rust/crates/tui/src/app.rs`

---

## Bug report (→ GitHub issue)

**What happens**

When a model isn't found in the bundled models.dev registry, the TUI falls back to a
provider-keyed default context window. The catch-all (and the `openai` arm) is 128 000:

```rust
self.context_window_size = match provider {
    "anthropic" => 200_000,
    "openai" => 128_000,
    "google" => 1_048_576,
    _ => 128_000,
};
```

For **local servers** (ollama / lmstudio / llamacpp), the real context window is whatever
the model was loaded with — its `num_ctx` — which is **not** in the models.dev catalog and
is frequently far smaller than 128 K (e.g. 8 K). Falling back to 128 K makes the context
meter report a window many times larger than reality, so "used %" looks tiny and never
warns as the real (small) window fills.

**Expected:** a conservative local default, and preference for an explicit registry entry
when one is supplied.

---

## PR

**Title:** fix(tui): conservative context-window fallback for local providers

**Summary**

Add a `8_192` fallback arm for the local-server providers (`ollama`, `lmstudio`/`lm-studio`,
`llamacpp`/`llama-cpp`) instead of letting them hit the 128 K catch-all. This under-promises
rather than over-promises, and an explicit registry entry (the authoritative source) still
wins. Leaves a TODO to read `/api/show` `parameters.num_ctx` from Ollama for an exact value.

> Note for danno: danno supplies an authoritative per-model registry entry via
> `CLAURST_MODELS_PATH`, so this fallback is the safety net, not the primary path.

**Diff**

```diff
diff --git a/src-rust/crates/tui/src/app.rs b/src-rust/crates/tui/src/app.rs
@@ -1875,11 +1875,17 @@ impl App {
         if let Some(entry) = self.model_registry.get(provider, model_id) {
             self.context_window_size = entry.info.context_window as u64;
         } else {
-            // Fallback: common defaults
+            // Fallback: common defaults. For local servers (ollama/lmstudio/llamacpp) the
+            // real window is whatever the model was loaded with (its `num_ctx`), which is
+            // NOT in the models.dev catalog and is often far smaller than a cloud default —
+            // assuming 128 K badly under-reports the meter. Use a conservative local default
+            // and prefer an explicit registry entry (the authoritative source; danno supplies
+            // one via CLAURST_MODELS_PATH). TODO: read `/api/show` parameters.num_ctx for ollama.
             self.context_window_size = match provider {
                 "anthropic" => 200_000,
                 "openai" => 128_000,
                 "google" => 1_048_576,
+                "ollama" | "lmstudio" | "lm-studio" | "llamacpp" | "llama-cpp" => 8_192,
                 _ => 128_000,
             };
         }
```

**Testing**
- A local Ollama model absent from the registry now reports an 8 K window instead of 128 K.
- A model present in the registry (or supplied via `CLAURST_MODELS_PATH`) is unchanged.

**Follow-up (not in this PR):** query Ollama `/api/show` for `parameters.num_ctx` to report
the exact loaded window instead of a conservative constant.
