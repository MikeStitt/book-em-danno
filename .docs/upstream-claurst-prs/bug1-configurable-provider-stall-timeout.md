# Bug 1 — Hard-coded 45 s provider stall timeout aborts slow local prefills

Branch: `fix/configurable-provider-stall-timeout` (commit `c435699`, off `upstream/main` 59c397f)
File: `src-rust/crates/query/src/lib.rs`

---

## Bug report (→ GitHub issue)

**Title:** Provider stall timeout is hard-coded to 45 s and aborts slow local-model prefills

**What happens**

`run_query_loop` guards the provider stream with a stall timeout that is hard-coded to
45 seconds:

```rust
let provider_stall_timeout = std::time::Duration::from_secs(45);
```

When a local Ollama model has a long **prefill** (large prompt / large context window),
the first streamed token can take longer than 45 s. Measured prefill latencies on
large-context Ollama models were **98–157 s** before the first chunk. With the 45 s cap:

1. The stall fires mid-prefill and the request is treated as stalled.
2. Retries are exhausted re-hitting the same slow prefill.
3. The turn is then assembled from **incomplete** stream data, which drops the in-progress
   tool-call block, producing a premature end-of-turn (the model "answers" without acting).

**Symptom seen by the user:** the agent appears to hang for ~45 s and then stops after one
turn, or silently fails to call a tool, on slower/local models — with no clear error.

**Expected:** the timeout should accommodate slow local prefills, or at least be
configurable for users running big local models.

**Environment:** local Ollama provider, large-context models; reproduced via danno's
headless `claurst -p` harness.

---

## PR

**Title:** fix(query): make provider stall timeout configurable (default 600 s)

**Summary**

Read the stall timeout from `CLAURST_PROVIDER_STALL_TIMEOUT_SECS`, defaulting to **600 s**
(up from the hard-coded 45 s). This keeps a stall guard for genuinely dead streams while no
longer aborting slow-but-healthy local prefills. Cloud providers are unaffected (their
first-token latency is well under either bound).

**Why 600 and not removed:** a stall guard still has value (a truly hung connection should
not block forever); 600 s comfortably covers the observed 98–157 s prefills with headroom,
and the env var lets operators tune it either way.

**Diff**

```diff
diff --git a/src-rust/crates/query/src/lib.rs b/src-rust/crates/query/src/lib.rs
@@ -1182,7 +1182,16 @@ pub async fn run_query_loop(
                     let mut msg_id = uuid::Uuid::new_v4().to_string();

                     use futures::StreamExt as ProviderStreamExt;
-                    let provider_stall_timeout = std::time::Duration::from_secs(45);
+                    // Configurable via CLAURST_PROVIDER_STALL_TIMEOUT_SECS (default 600).
+                    // The old hard-coded 45s aborted slow local prefills (98-157s measured on
+                    // large-context Ollama models), exhausting retries and building the turn from
+                    // incomplete stream data -> dropped tool-call blocks -> premature end-of-turn.
+                    let provider_stall_timeout = std::time::Duration::from_secs(
+                        std::env::var("CLAURST_PROVIDER_STALL_TIMEOUT_SECS")
+                            .ok()
+                            .and_then(|v| v.parse().ok())
+                            .unwrap_or(600),
+                    );
                     let provider_stall = tokio::time::sleep(provider_stall_timeout);
                     tokio::pin!(provider_stall);
                     let mut provider_stream_stalled = false;
```

**Testing**
- Local Ollama large-context model that previously stalled at 45 s now completes its
  prefill and streams normally; tool calls survive into the turn.
- `CLAURST_PROVIDER_STALL_TIMEOUT_SECS=10` restores fast-fail behavior for testing.
