# Bug 5 — Context meter "used" never advances for Ollama / NVIDIA NIM (missing stream usage request)

Branch: `fix/ollama-nvidia-stream-usage` (commit `f384b57`, off `upstream/main` 59c397f)
File: `src-rust/crates/api/src/providers/openai_compat_providers.rs`

> ⚠️ **The fork branch also accidentally committed a built binary
> (`claurst-linux-arm64`) and a tarball. EXCLUDE those from the upstream PR.** Cherry-pick
> only the source change below (`openai_compat_providers.rs`).

> **⚠️ DEPENDS ON BUG 8 — do not submit standalone.** Making usage flow for local
> providers is precisely what *arms* the auto-compact trigger: today the frozen-at-0
> meter is (accidentally) the only thing keeping the ≥ 99% livelock dormant for
> Ollama/NIM. With this patch and an unlatched trigger, any session whose real usage
> crosses 99% of the (possibly fallback-guessed — Bug 4) window dispatches unprompted
> compact turns forever (observed live 2026-07-10). Re-parent this branch onto
> `fix/auto-compact-safety` and declare the dependency in the PR text. See
> [bug8-auto-compact-unsafe-trigger.md](bug8-auto-compact-unsafe-trigger.md) and README.

---

## Bug report (→ GitHub issue)

**What happens**

The OpenAI-compatible streaming endpoints for **Ollama** and **NVIDIA NIM** only emit a
final `usage` chunk when the request sets `stream_options.include_usage = true`. The
`ollama()` and `nvidia()` provider builders don't request it, so no usage chunk arrives
during streaming and the TUI's context-meter "used" token count **never advances** — it
stays at 0 (or its initial value) for the whole session on these providers.

**Expected:** the context meter's "used" count should advance as tokens are consumed, the
same as it does for providers that do emit usage.

---

## PR

**Title:** fix(providers): request streamed token usage for Ollama and NVIDIA NIM

**Summary**

Set `include_usage_in_stream: true` on the `ollama()` and `nvidia()` provider builders so
they send `stream_options.include_usage` and receive the final usage chunk during
streaming. This makes the context meter's "used" count work on both providers.

(Pairs naturally with Bug 6, which fixes how that usage number is then applied to the meter.)

**Diff**

```diff
diff --git a/src-rust/crates/api/src/providers/openai_compat_providers.rs b/src-rust/crates/api/src/providers/openai_compat_providers.rs
@@ -70,6 +70,10 @@ pub fn ollama() -> OpenAiCompatProvider {
             "exceeded.*context length".to_string(),
         ],
         no_api_key_required: true,
+        // Ollama's OpenAI-compat endpoint only emits a final usage chunk when
+        // stream_options.include_usage is requested; without it the context
+        // meter's "used" count never advances.
+        include_usage_in_stream: true,
         ollama_native_host: Some(host),
         ..Default::default()
     })
@@ -332,6 +336,12 @@ pub fn nvidia() -> OpenAiCompatProvider {
         "https://integrate.api.nvidia.com/v1",
     )
     .with_api_key(key)
+    // NIM honors stream_options.include_usage; request it so the context
+    // meter's "used" count advances during streaming.
+    .with_quirks(ProviderQuirks {
+        include_usage_in_stream: true,
+        ..Default::default()
+    })
 }
```

**Testing**
- Ollama: context meter "used" advances during/after a streamed turn (was frozen at 0).
- NVIDIA NIM: usage chunk now arrives; meter advances. (Verified against live NIM.)
