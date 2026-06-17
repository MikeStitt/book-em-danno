# Connecting Claude Code to non-Anthropic models — investigation

> **Living document.** Captured knowledge from an investigation (June 2026) into
> running Claude Code against non-Anthropic models, prompted by wanting to drive
> NVIDIA Nemotron through danno's sandbox. `.docs/` is **exempt from
> markdown/format quality checks**, so this is a free-form knowledge dump, not a
> spec. Verify version-specific claims (provider endpoints, model names, gateway
> behavior) before relying on them — this space moves fast.

## TL;DR

- **`danno.toml` configures OpenCode, not Claude Code.** The `[models]`/`[agents]`
  blocks generate `.opencode/opencode.jsonc`, which only the **opencode** agent
  reads. The **claude** agent is Claude Code, which speaks the **Anthropic
  Messages API** and ignores `opencode.jsonc` entirely.
- To drive Claude Code with a non-Anthropic model you need an **Anthropic
  `/v1/messages` endpoint** in front of that model. Two ways to get one:
  1. a **provider that natively exposes** an Anthropic-compatible endpoint
     (cleanest — the vendor owns/tunes the translation), or
  2. a **translating gateway/proxy you run** (LiteLLM, claude-code-router, …).
- **NVIDIA NIM (`integrate.api.nvidia.com`) is OpenAI-compatible only — no native
  Anthropic endpoint.** So Nemotron-via-Claude-Code requires a self-run proxy or
  an aggregator that hosts Nemotron behind an Anthropic skin. The low-risk path
  for Nemotron is **OpenCode** (OpenAI-native, zero translation) — which danno
  already configures via the `openai` backend kind.
- "Tool-use fidelity" is **two** problems, not one: (A) the gateway translating
  the tool-call wire format, and (B) the model's general agentic competence.
  Most real failures are (B), not "the model doesn't know Claude Code's tools."

## 1. Why this is hard: the protocol boundary

Claude Code only speaks the **Anthropic Messages API** (`POST /v1/messages`, plus
`/v1/messages/count_tokens`). Non-Anthropic model APIs (OpenAI, NVIDIA NIM, most
local servers) speak the **OpenAI Chat Completions** format. The shapes differ in
ways that matter for an agentic tool like Claude Code:

- Anthropic: tool calls/results are **content blocks** (`tool_use` / `tool_result`)
  with `toolu_…` IDs; tool defs use `input_schema`.
- OpenAI: tool calls are `tool_calls` with `tool_call_id`, results are
  `role:"tool"` messages; tool defs use `parameters`.

Something has to translate between them. Claude Code itself won't — so the bridge
lives in a gateway/endpoint.

## 2. Claude Code's gateway env vars (official)

From the official docs (`code.claude.com/docs/en/llm-gateway`,
`/docs/en/model-config`):

| Variable | Purpose |
|---|---|
| `ANTHROPIC_BASE_URL` | Point Claude Code at the gateway/endpoint (it appends `/v1/messages`). |
| `ANTHROPIC_AUTH_TOKEN` | Sent as `Authorization: Bearer …` — preferred for gateways. |
| `ANTHROPIC_API_KEY` | Alternative; sent as the `x-api-key` header. Set to `""` for aggregators (e.g. OpenRouter) so CC doesn't try to auth against Anthropic. |
| `ANTHROPIC_MODEL` / `ANTHROPIC_SMALL_FAST_MODEL` | The model IDs the endpoint maps. Set **both** — the "small/fast" path (titles, summaries) errors if unset. |
| `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` | Sometimes needed for non-Anthropic backends. |
| `CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1` | Opt-in `/v1/models` discovery (v2.1.129+); only adds models whose IDs start with `claude`/`anthropic`, so set `ANTHROPIC_MODEL` manually otherwise. |

Gateway **requirements**: expose `/v1/messages` (+ `/v1/messages/count_tokens`) and
**forward the `anthropic-beta` and `anthropic-version` headers** (dropping them
silently reduces functionality). Claude Code also sends `X-Claude-Code-Session-Id`
and agent-id headers a proxy can use for cost attribution. First-class flags exist
for `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` (Claude models on your
cloud — native by definition, but they serve Claude, not other models).

## 3. "Tool-use translation fidelity" — the two layers

This is the crux of whether a non-Anthropic setup actually *works* for coding.

**Misconception to kill:** it is **not** that "other models don't know Claude
Code's tool calls." Claude Code's tools (`Bash`, `Read`, `Edit`, `Write`, `Grep`,
`TodoWrite`, …) are **ordinary JSON-Schema function-calling tools** sent in each
request. No model has Claude-Code-specific knowledge; any strong function-caller
can invoke them. The difficulty splits into two independent layers:

### Layer A — protocol/wire translation (the gateway's job)

Mostly solved by mature gateways, but with real sharp edges:

1. **Streaming event-ID mismatches.** Anthropic SSE uses stable block IDs; a naive
   translator emits deltas whose IDs were never "opened," and the client drops
   them. Live example: LiteLLM #26529 — proxying Claude through the
   OpenAI-compatible path breaks multi-step tool calls with `"text part {id} not
   found"`.
2. **`tool_use_id` ↔ `tool_call_id` round-tripping.** Lose the correspondence and
   the model can't tell which result answers which call → loops / re-calls.
3. **Parallel tool calls.** Several `tool_use` blocks in one turn must all survive
   with correct ordering; weak shims collapse them.
4. **Partial-JSON argument reassembly.** Mis-buffered streaming args → truncated /
   empty tool inputs ("called Edit with no arguments").
5. **Dropped `anthropic-beta`/`anthropic-version`** → silently changed behavior
   (fine-grained tool streaming, token-efficient tools).
6. **Tool-schema dialect & strict mode** (`input_schema` vs `parameters`;
   OpenAI strict / `additionalProperties`).

**Best-in-class fix:** a *structured intermediate representation* — parse the
Anthropic request into a neutral model, re-serialize to the upstream, preserving
parallel blocks, `cache_control`, and streaming event types — not naive string
rewriting. **The surest fix is to eliminate Layer A** (see §5/§6).

### Layer B — model agentic competence (the model's job)

Where most real-world failures live, and it's *general* skill:

- Emitting well-formed JSON matching the schema **every** time (one bad call
  derails a session).
- Knowing **when** to call a tool; not stopping early; not looping.
- Sustaining a **multi-step loop** (read → edit → run → observe → fix) over dozens
  of turns.
- Honoring **strict-format tools** — Claude Code's `Edit` needs an *exact*
  `old_string` match; weak models approximate and fail.
- Following Claude Code's **large, Claude-tuned system prompt** + staying coherent
  over long context.

"Can function-call on a benchmark" ≠ "survives a 30-tool-call coding session."
**Select empirically:** run a real multi-step task (read several files, multi-edit
change, run tests, fix a failure) and watch for malformed calls, early stops, or
loops. A one-shot prompt won't reveal it.

## 4. Translating gateways you run (when no native endpoint exists)

| Gateway | Notes |
|---|---|
| **LiteLLM** | Most complete/enterprise; "unified" `/v1/messages` endpoint is what Anthropic's docs walk through; cost tracking, fallbacks, 100+ providers. **Security:** PyPI **1.82.7 / 1.82.8 shipped credential-stealing malware** — pin a clean version, rotate keys if touched. Anthropic does not endorse/audit it. Has documented streaming edge bugs (#26529). |
| **claude-code-router** | Purpose-built for Claude Code; per-task routing (cheap model for background, big model for reasoning). Community project — not a clean replacement for CC's official login/support path. |
| **Bifrost** | Fastest at production scale (~11µs overhead, thousands req/s). |
| **claude-code-proxy** variants (1rgs, nielspeter, fuergaosi233) / **y-router** / **anthropic-proxy** | Lightweight single-purpose proxies. Purpose-built CC proxies tend to handle tool-call edges better than generic OpenAI-compat shims. |

Use the unified `/v1/messages` endpoint (not pass-through) for non-Anthropic
backends. Treat any gateway as a **secret-bearing service** (it holds your real
provider key).

LiteLLM `config.yaml` for NVIDIA Nemotron (NIM is OpenAI-compatible):

```yaml
model_list:
  - model_name: claude-sonnet-4-5            # the alias Claude Code requests
    litellm_params:
      model: openai/nvidia/nemotron-3-ultra-550b-a55b
      api_base: https://integrate.api.nvidia.com/v1
      api_key: os.environ/NVIDIA_API_KEY
```
```bash
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_AUTH_TOKEN=sk-litellm-key
export ANTHROPIC_MODEL=claude-sonnet-4-5
claude
```

## 5. Providers with a *native* Anthropic `/v1/messages` endpoint

The cleanest path: the provider implements the Anthropic Messages API server-side,
so you set `ANTHROPIC_BASE_URL` and you don't run/secure/debug a shim. (For
non-Claude models, translation still happens — but it's the vendor's maintained,
Claude-Code-tuned implementation. True *zero* translation only exists for Claude
itself.)

**A. Model providers with an Anthropic skin** (flexible-on-model = cleanest):
- Moonshot / Kimi → `https://api.moonshot.ai/anthropic`
- MiniMax → `https://api.minimax.io/anthropic`
- Alibaba Cloud Model Studio / Qwen (DashScope) →
  `https://dashscope-intl.aliyuncs.com/apps/anthropic` (set `ANTHROPIC_MODEL=qwen…`)
- DeepSeek, Z.AI / GLM (coding plan) — Anthropic-compatible endpoints.
- Maintained matrix: `github.com/Alorse/cc-compatible-models` (DeepSeek, Qwen,
  MiniMax, Kimi, GLM, MiMo, StepFun — pricing + configs).

**B. Aggregator with a native Anthropic skin** (one endpoint, many models):
- **OpenRouter** → `ANTHROPIC_BASE_URL=https://openrouter.ai/api` (set
  `ANTHROPIC_API_KEY=""`, key in `ANTHROPIC_AUTH_TOKEN`). 200+ models; per-model
  translation fidelity varies.

**C. Inference platforms that added the Messages API:**
- **FriendliAI** — open-weight models (GLM, MiniMax, Kimi, DeepSeek families) via
  the Anthropic Messages API on serverless + dedicated endpoints.

**D. Self-host with a built-in Messages API (best for local):**
- **llama.cpp** now ships a **native Anthropic Messages API endpoint** — run
  `llama-server` with your own open model and point Claude Code straight at it,
  **no separate proxy**. The local analogue of everything above.

**E. Native by definition (Claude models only):** Anthropic API, Amazon Bedrock
(`CLAUDE_CODE_USE_BEDROCK`), Google Vertex (`CLAUDE_CODE_USE_VERTEX`).

## 6. The NVIDIA Nemotron situation (the original goal)

- NVIDIA NIM is **OpenAI-compatible only — no native Anthropic endpoint**, so §5
  doesn't directly cover it.
- Options for Nemotron specifically:
  1. Route it through an **aggregator with an Anthropic skin** that *hosts*
     Nemotron (check OpenRouter/FriendliAI coverage).
  2. Run a **self-managed translating proxy** (§4).
  3. **Use OpenCode**, which talks NIM natively — **no translation at all**. This
     is what danno configures via the `openai` backend kind; only Layer-B model
     competence remains.
- If model choice is flexible, the §5-A providers (GLM, Kimi, Qwen, MiniMax,
  DeepSeek) give the cleanest *Claude-Code-with-a-non-Anthropic-model* experience
  because the vendor maintains the endpoint.

## 7. danno / sandbox-specific considerations

- **Egress policy:** the sandbox allows **public internet** but denies host/LAN
  except the Ollama allow-host hole. Consequences:
  - Any **public** provider endpoint (§5 A–C, NVIDIA NIM) is reachable from the
    sandbox with **no allow-host rule**.
  - A **host-local** gateway (LiteLLM on your Mac, self-hosted llama.cpp) is
    host/LAN → needs an allow-host rule (like `--allow-host localhost:11434` for
    Ollama) **or** must run inside the VM.
- **Running the gateway *inside* the sandbox VM** (investigated): viable and neat
  because **claude → gateway is intra-VM loopback** (not subject to the egress
  proxy) and **gateway → public API is allowed** — so no allow-host rule needed.
  Cost: install the gateway into the VM at runtime (VM has egress; `docker sandbox
  exec --detach` to start it + a readiness wait), it's ephemeral per VM (reinstall
  on rebuild, or bake via `docker sandbox save`), and harder to debug. *Assumption
  to verify live:* that pure in-VM loopback isn't intercepted by danno's proxy
  (which does a `host.docker.internal→localhost` rewrite for egress).
- **Host gateway + allow-host rule** is more consistent with danno's existing
  host-Ollama pattern, persistent, and easier to operate — likely the better
  *danno feature* shape.
- danno's **`{env:VAR}` fail-loud check** (added on
  `feat-openai-backend-and-provision-fix`) already covers injecting/validating an
  endpoint's API key at `sandbox start` for the opencode path.

## 8. Possible danno features (not yet built)

- **`kind = "anthropic"` backend** for the `claude` agent: `base_url` +
  `api_key_env` + model id, wiring `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` /
  `ANTHROPIC_MODEL` into the sandbox launch via the existing env-injection +
  fail-loud machinery. Points Claude Code at a native-Anthropic-endpoint provider
  (§5) or a gateway.
- **In-sandbox or host gateway orchestration** (§7) for models without a native
  Anthropic endpoint (e.g. Nemotron via LiteLLM), incl. an allow-host lever.

## 9. Best-practice checklist

1. **Minimize Layer A first** — prefer a native-Anthropic-endpoint provider, or an
   OpenAI-native agent (OpenCode/Aider), over an Anthropic↔OpenAI translation.
2. If you must translate, use a **purpose-built, maintained** CC proxy with a
   structured IR and explicit tool-call tests; **pin a clean version**.
3. **Forward `anthropic-beta`/`anthropic-version`**; enable tool-streaming betas.
4. **Test tool round-trips explicitly**: parallel calls, streamed partial args,
   `tool_result` mapping — not a "hello world" call.
5. **Choose an agentic-grade model**; validate on a real multi-step task.
6. Keep a **generous context window** (CC's system prompt + tool schemas are big).
7. Treat the gateway as **secret-bearing**; mind the `ANTHROPIC_API_KEY=""` auth
   gotcha on aggregators.

## Sources

- Claude Code: `code.claude.com/docs/en/llm-gateway`, `/docs/en/model-config`
- LiteLLM non-Anthropic guide: `docs.litellm.ai/docs/tutorials/claude_non_anthropic_models`;
  streaming/tool bug: `github.com/BerriAI/litellm/issues/26529`
- Providers / matrices: `friendli.ai/blog/friendliai-supports-anthropic-messages-api`,
  `alibabacloud.com/help/en/model-studio/claude-code`,
  `openrouter.ai/docs/cookbook/coding-agents/claude-code-integration`,
  `github.com/Alorse/cc-compatible-models`, `huggingface.co/blog/ggml-org/anthropic-messages-api-in-llamacpp`
- Proxies: `github.com/sunflower0305/claude-proxy`, claude-code-router, Bifrost,
  `github.com/1rgs/claude-code-proxy`, `github.com/nielspeter/claude-code-proxy`,
  `github.com/luohy15/y-router`
- Comparisons: `requesty.ai/blog/agentic-coding-tools-compared-2026-…`,
  `getmaxim.ai/articles/5-ai-gateways-…`, `futureagi.com/blog/best-ai-gateways-claude-code-…`
