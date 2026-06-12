# Research: Ollama `thinking`/`think`, and whether danno's `opencode.jsonc` passes it through

**Date:** 2026-06-12
**Status:** Draft for peer review
**Author:** Claude (Opus 4.8) session, at Mike Stitt's request
**Scope:** Two linked questions that arose while reviewing danno's generated
`opencode.jsonc` against a hand-written reference (`temp/opencode.jsonc`):

1. For Ollama, does `thinking=false` (Ollama's `think` param) change an AI model's
   answers?
2. Is danno placing `thinking`/`stream`/`num_ctx` in the **correct** `opencode.jsonc`
   keys so they actually reach Ollama? (Prompted by a Google AI Overview claiming the
   keys are wrong.)

> **Why this matters now.** On branch `live-permutation-tests` we just shipped a
> generator change (`OllamaBackend.{stream,thinking,output_limit}` →
> `provider.<id>.options.{stream,thinking}` and `models.<tag>.limit.output`). The
> research below suggests two of those three keys are likely **inert** as placed.
> This document is the evidence base for deciding whether to restructure that change.

---

## 1. Question A — does `think=false` change the model's answers?

### Findings

- **`think` is a real Ollama runtime parameter** (boolean for most reasoning models).
  Default is **enabled** for supported models in both CLI and API. Disabling it
  injects a control token instructing the model to skip its chain-of-thought (CoT)
  and answer directly. [Ollama Thinking docs; Ollama blog]
- **Only some models support it.** Reasoning models: Qwen 3, DeepSeek-R1,
  DeepSeek-v3.1, GPT-OSS. Non-reasoning models (gemma3/gemma4, llama, mistral, …)
  have no thinking phase, so the parameter is a **no-op** for them. [Ollama docs]
- **GPT-OSS is special:** it expects `low`/`medium`/`high` (trace length), **cannot**
  be fully disabled, and ignores a boolean `true`/`false`. [Ollama docs / web search]

### Does it change the *answer*?

Two layers, and the distinction is the crux:

- **Official framing (display switch):** Ollama docs say `think=false` suppresses the
  *visible reasoning trace*; the final answer still lands in `message.content`. By
  that framing the answer field is "unchanged."
- **Mechanical reality (computation switch):** `think=false` doesn't just hide CoT —
  it **prevents the model from generating it**. For a true reasoning model the CoT is
  *how* it computes the answer. So:
  - Easy/lookup prompts → answers usually equivalent, just faster/cheaper.
  - Multi-step prompts (math, logic, planning, structured output) → answers are often
    **measurably worse**, and structured outputs (e.g. JSON schemas) can become
    **less reliably formatted**. The Google AI Overview corroborated this
    (Reddit observation: little difference on simple answers; unpredictable
    formatting for structured outputs when reasoning is suppressed).

### Conclusion A

**Yes — for reasoning models, `think=false` can change answer content/quality, not
just verbosity.** For non-reasoning models (incl. the `gemma4:26b` in our reference
config) it does nothing. The Ollama docs' "doesn't alter the final answer" is true
only in the narrow sense that the answer *field* is still populated.

---

## 2. Question B — is danno using the right `opencode.jsonc` keys?

A Google AI Overview claimed `"thinking": false` under `provider.<id>.options` "will
have no effect." Independent verification against opencode docs, a worked community
config, and GitHub issues **confirms the core claim.**

### opencode has two distinct option scopes

| Scope | Config path | What it's for | Accepts |
|---|---|---|---|
| Provider-level | `provider.<id>.options` | passed to the `@ai-sdk/openai-compatible` **constructor** | `baseURL`, `apiKey`, `headers` |
| Model-level | `provider.<id>.models.<m>.options` | per-request settings | incl. **`extraBody`** for arbitrary request-body fields |

**Custom, non-OpenAI keys in provider-level `options` are dropped** before the request
leaves opencode (the SDK only forwards what it recognizes). This is a filed bug class,
not speculation:
- "Custom OpenAI-compatible provider options not being passed …" (opencode/AI SDK)
- "`think: false` option not working for Ollama models #3755"

### Worked, proven mechanism: model-level `extraBody`

A real community config for `gpt-oss` in opencode places reasoning at **model level**:

```json
"provider": {
  "ollama": {
    "npm": "@ai-sdk/openai-compatible",
    "options": { "baseURL": "http://localhost:11434/v1", "apiKey": "ollama" },
    "models": {
      "gpt-oss-20b-high-32k": {
        "id": "gpt-oss-20b-32k",
        "options": { "extraBody": { "think": "high" } }
      }
    }
  }
}
```

Key facts from that source [nijho.lt]:
- Reasoning (`think`) goes under `models.<m>.options.extraBody` — forwarded per request.
- **`num_ctx` is NOT set in opencode at all.** Ollama's OpenAI-compatible `/v1` API
  cannot set the context window; you bake it into an Ollama model variant
  (`/set parameter num_ctx …` → `/save`, or a `Modelfile`), then point opencode at
  that model name.

### Per-key verdict for what danno currently emits

| Key (as danno emits it) | Current location | Correct location | Verdict |
|---|---|---|---|
| `thinking` (bool) | `provider.options.thinking` | model-level `options.extraBody.think` (bool for qwen/deepseek; string for gpt-oss) | **Wrong name + wrong scope → inert** |
| `num_ctx` | `provider.options.num_ctx` | **not expressible in opencode.jsonc**; Modelfile/variant only | **Inert** for the real window |
| `stream` | `provider.options.stream` | not a documented opencode key (streams by default) | **Likely inert** |
| `limit.context` / `limit.output` | model-level `limit` | model-level `limit` | ✅ **Correct** (client-side budget) |

### Evaluation of the Google AI Overview's proposed fixes

- **Modelfile variant (its Option 2)** — *Solid.* Version-proof; also the only real way
  to set `num_ctx`. Recommended.
- **`providerOptions: { "openai-compatible": { think: false } }` (its Option 1)** —
  *Plausible but unverified for opencode.* Prefer the proven `extraBody` path.
- **`reasoningSummary: "none"` (its Option 3)** — *Speculative* ("if your build
  supports it"). Do not rely on it.

### Conclusion B

**The Google AI Overview is correct on its central point:** `provider.options.thinking`
(and by the same logic `stream`/`num_ctx`) does not reach Ollama. The correct,
proven placement for reasoning control is **model-level `options.extraBody.think`**;
the real context window is a **Modelfile/variant** concern, not an `opencode.jsonc`
key. Only `limit.context`/`limit.output` were placed correctly.

---

## 3. Cross-cutting operational risk: the `reasoning`-field hang (opencode #21903)

Independent of key placement: when an Ollama model returns a generic **`reasoning`**
field, opencode's Zod schema (which only accepts Copilot's `reasoning_text` /
`reasoning_opaque`) **rejects it and spins forever at high CPU with no output.**
Reported against Ollama ≥ 0.20.4 with models like `gemma4`/`qwen3.5`. Trigger is the
*presence of the field in the response*, not the act of toggling thinking. Current
workaround is a code patch to the schema; no pure-config fix is documented.

**Implication:** enabling reasoning on an Ollama model under opencode is currently
risky regardless of where the key lives. A `think`-disabling **Modelfile variant** is
the most robust mitigation today.

---

## 4. Implications for danno (what we shipped on `live-permutation-tests`)

1. The generator change emits `provider.options.{stream,thinking}` and a
   `num_ctx` that — per the evidence — **do not affect Ollama**. Only the
   `limit.output` portion (now configurable) is on solid ground.
2. This **contradicts the working assumption** ("live testing confirmed `stream` and
   `thinking` both matter"). The most likely reconciliation: the observed effect came
   from something else (a model variant's real `num_ctx`, or `limit.context`), not
   from these provider-level keys.
3. The **slow e2e permutation tests** that "validated opencode permutations" should be
   re-examined — they may be asserting on keys that are inert.
4. Documentation we just added to `README.md` / `_HEADER` / `danno.toml.example`
   describing `provider.options.num_ctx` as the real window is **half-wrong** and
   should be corrected if the conclusions here hold.

### Proposed generator shape (pending empirical confirmation, §5)

- **Keep** `limit.context` / `limit.output` (real, client-side, correctly placed).
- **Drop** `stream` and `num_ctx` from `provider.options`; document that the true
  context window is set via an Ollama Modelfile/variant.
- **Move** thinking control to model-level `options.extraBody.think`, emitted **only**
  for reasoning-capable models, value typed per model (bool vs `low`/`medium`/`high`).
- Consider a guard/Modelfile recommendation for the `#21903` hang.

---

## 5. Future investigations (open questions)

1. **[Decisive] Request-body capture.** Run opencode against an Ollama **reasoning**
   model (e.g. qwen3) and capture the actual HTTP body Ollama receives — via Ollama
   server logs or a small intercepting proxy — for three configs:
   (a) key at `provider.options`, (b) key at model `options.extraBody`,
   (c) `providerOptions.openai-compatible`. This definitively shows what opencode
   forwards and settles the conflict with the prior live result.
2. **opencode version sensitivity.** Provider-option handling and the `#21903` schema
   may differ across opencode builds. Pin the version used for danno's e2e tests and
   record it; re-test on upgrade.
3. **`stream` semantics.** Confirm whether opencode exposes any per-provider stream
   toggle at all, or whether streaming is always on. If always on, stop emitting it.
4. **`num_ctx` strategy.** Decide whether danno should (a) only document the Modelfile
   path, or (b) optionally *generate* a Modelfile/variant and create it via
   `ollama create` during `install`. (b) is a larger feature; out of current scope.
5. **gemma reality check.** Confirm empirically that `gemma4:26b` emits no `reasoning`
   field (so it neither benefits from `think` nor risks the `#21903` hang), to justify
   leaving thinking-control off for non-reasoning backends.
6. **`extraBody` value typing.** Verify opencode forwards a **boolean** `think` (qwen/
   deepseek) and a **string** `think` (gpt-oss) unchanged through `extraBody`.

---

## Sources

- Ollama — Thinking (docs): https://docs.ollama.com/capabilities/thinking
- Ollama — Thinking (blog): https://ollama.com/blog/thinking
- Bas Nijholt — gpt-oss in opencode, larger context & high reasoning (worked config,
  `extraBody`, num_ctx-via-variant): https://www.nijho.lt/post/ollama-opencode/
- opencode #21903 — openai-compatible rejects Ollama `reasoning` field → infinite spin:
  https://github.com/anomalyco/opencode/issues/21903
- opencode #3755 — `think: false` not working for Ollama models (referenced via search)
- "Custom OpenAI-compatible provider options not being passed" (referenced via search)
- opencode providers docs: https://opencode.ai/docs/providers/
- p-lemonish/ollama-x-opencode — tool-capable Ollama setup guide:
  https://github.com/p-lemonish/ollama-x-opencode
- DeepCharts — enable/disable model thinking in Ollama:
  https://deepcharts.substack.com/p/how-to-use-or-disable-model-thinking
- Google AI Overview responses (2026-06-12), provided by the user, used as a claim to
  verify (not as a primary source).

### Confidence notes for reviewers

- **High confidence:** provider-level custom options are stripped; `extraBody`
  (model-level) is the proven mechanism; `num_ctx` not settable via `/v1`; gemma is
  non-reasoning; param is `think` not `thinking`.
- **Medium confidence:** exact behavior of `providerOptions.openai-compatible` and
  `reasoningSummary` in the specific opencode build danno targets (version-dependent).
- **Unresolved until §5.1:** why the earlier live permutation testing appeared to show
  `stream`/`thinking` mattering. Do not overwrite that empirical result on docs alone.
