# Claurst + qwen3-coder-next: tool-calling investigation, model & KV-cache research

_Investigation date: 2026-06-25. Host: MacBook Pro, 64 GB unified memory. Ollama 0.30.6._

Context: a user driving **claurst** (a Rust Claude-Code clone) as an interactive
agent in a danno `shell` sandbox against local Ollama `qwen3-coder-next` reported
that the agent "stops without a result," and that telling it to "continue" makes it
resume working. This doc records the diagnosis, the model research that followed, and
the model that was pulled as a result.

## 1. Symptom, confirmed from claurst's own logs

claurst persists sessions under the relocated sandbox `$HOME`, which danno maps to
the per-project agent-home on the host:

```
/Users/mikestitt/.danno/agent-home/danno-temp-claurst-project-claurst/.claurst/
  sessions/<session-id>.json     # full transcript
  settings.json                  # model + permission rules
  todos/<session-id>.json
```

Walking the transcript (83 messages) showed a clear split:

| Messages | What claurst stored | Effect |
| --- | --- | --- |
| broken stretch | assistant `content` is a **plain string** preamble ("Let me read the key source files:"), **0 tool calls**, no completion id | turn ends → looks "stopped"; user nudges "continue" |
| working stretch | assistant `content` is a **list** with real `tool_use` blocks + a `uuid: chatcmpl-NNN` | tools run, work proceeds |

So the model emitted a sentence *announcing* an action, the response ended with
`finish_reason: "stop"`, claurst's agent loop saw no tool call, and it handed control
back to the user. "Continue" eventually produces a real tool call. This is
**narrate-then-stop**, and claurst has no recovery for it (it does not re-prompt the
model to actually emit the tool call).

## 2. What was ruled out

- **The danno relay** (`driver.py`, the in-VM `127.0.0.1:11434` → host-Ollama bridge):
  byte-transparent re-chunking; complete, well-formed responses arrive when a tool
  call is actually emitted.
- **claurst's parser / Ollama's tool template:** direct probes against host Ollama
  returned **clean structured `tool_calls` with ids 9/9 times** (1 tool, 12 tools,
  streaming and non-streaming, even with a primed narrate-then-stop history at
  temp 1.0). claurst's streaming path (`openai_compat.rs::create_message_stream`)
  parses those correctly; the working messages prove it.
- **Sampling / template misconfig:** the model's Ollama params are
  `temperature 1, top_k 40, top_p 0.95` — **exactly** Qwen's recommended best-practice
  values. The bare `{{ .Prompt }}` template is the official one and tool calling works
  through it (Ollama reports `Capabilities: tools`). Nothing is *mis*configured.

**Conclusion:** there is no hard parsing bug. The model intermittently chooses to
narrate instead of act — an agentic-discipline lapse that a low quant of a model with
only **3B active parameters** is prone to. The leverage is therefore **higher-fidelity
weights** (a better quant) plus a build that bundles the latest tool-call parsing
fixes — not a config change.

> Last unknown — now CAPTURED + CLOSED (2026-06-26). `--capture --harness claurst` now
> records claurst↔Ollama traffic through the existing capture proxy (relay pointed at it
> via `DANNO_RELAY_UPSTREAM_PORT`). A headless capture of the failing README-review
> prompt (`scratch/capture_claurst_turn_only.py`) settled it:
>
> - **claurst's request is correct.** Every `/v1/chat/completions` carries the **full 45
>   tools** (Bash, Read, Edit, Write, Glob, Grep, … Agent) AND a **4783-char system
>   prompt** ("You are Claurst, Anthropic's official CLI for Claude. ## Capabilities …").
>   `stream: true`. So narrate-then-stop is **NOT** claurst dropping tools or sending a
>   degraded request — that hypothesis is dead.
> - **The failure is stochastic, model-side.** This headless run did NOT reproduce the
>   stall: the model narrated *and acted*, 12 tool calls across 11 round-trips, `ok=True`.
>   It emitted the very same phrases the user saw ("Now let me review the source files:")
>   but here followed them with real tool calls. The interactive stalls are the model
>   (qwen3-coder-next Q4, 3B-active, temp 1.0) sometimes emitting a **pure-narration
>   response with no tool call**, which claurst correctly treats as turn-end → returns to
>   the user. A long session reinforces it (accumulated narration-only assistant turns).
> - **Therefore the lever is the weights**, not config or claurst: the Unsloth UD-Q4_K_M
>   pull (better dynamic quant + tool-call fixes) is the right fix to reduce the stall
>   rate. claurst could also be hardened to re-prompt on narration-without-action, but
>   that is claurst's design, not danno's.
>
> Aside — a real danno install bug surfaced: `install_claurst`'s plain
> `curl -fsSL --max-time 180` cannot survive the squid egress proxy **truncating** the
> GitHub release download (`curl: (18) transfer closed`), which is intermittent here. A
> resumable retry (`curl --retry 5 --retry-all-errors -C -`) recovers cleanly. Worth a
> follow-up fix to `src/danno_validator/claurst.py`.

## 3. What qwen3-coder-next is

[Qwen3-Coder-Next](https://huggingface.co/Qwen/Qwen3-Coder-Next): an MoE coding model
built on Qwen3-Next — **79.7B total / 3B active**, 256K context, non-thinking,
agentically trained for tool use. The user's existing model is the **official Ollama**
`qwen3-coder-next:latest` (52 GB, Q4_K_M, 262144 ctx) — correctly tool-capable, not
broken. Because only 3B params are active, quantization noise hits the active path
harder than on a dense model, which is exactly where "always emit the tool call"
discipline degrades.

Recommended sampling (Qwen model card): `temperature=1.0, top_p=0.95, top_k=40`.

## 4. Quant options (Unsloth GGUF) and the 64 GB budget

Source: [unsloth/Qwen3-Coder-Next-GGUF](https://huggingface.co/unsloth/Qwen3-Coder-Next-GGUF).
The Unsloth repo ships the up-to-date **chat-template + tool-call parsing fixes** and
**Dynamic 2.0 (UD)** quants, which keep important layers at higher precision (better
quality per byte than a stock same-size quant).

Memory reality on 64 GB: the host Ollama model, the Docker sandbox VM, and macOS all
share the 64 GB. The current 52 GB model already runs but leaves only ~12 GB for
everything else. Practical envelope while the sandbox is up: **stay at/below ~50 GB of
weights**, which rules out Q5/Q6/Q8.

Selected quant sizes (weights only):

| Quant | Size | Fit on 64 GB w/ sandbox |
| --- | --- | --- |
| UD-Q3_K_XL | 36.3 GB | roomy (fallback if memory pressure) |
| UD-IQ4_XS | 38.4 GB | roomy |
| **UD-Q4_K_M** | **49.3 GB** | **recommended — same footprint as current, better quality + fixes** |
| UD-Q4_K_XL | 49.6 GB | ok |
| Q5_K_M / UD-Q5_K_XL | 56.8 / 59.5 GB | too tight (starves Docker + macOS) |
| Q6_K / Q8_0 | 65.8 / 84.8 GB | does not fit |

**Decision: `UD-Q4_K_M` (49.3 GB)** — best quality runnable on 64 GB alongside the
sandbox, drop-in at the existing footprint.

## 5. Context capacity & KV cache (computed from the real architecture)

Qwen3-Coder-Next is a **hybrid** architecture (`config.json`):

- `num_hidden_layers: 48`, `full_attention_interval: 4` → only **12 of 48 layers are
  full attention**; the other 36 are linear DeltaNet layers whose recurrent state is
  **fixed-size and does not grow with context** (~40 MB total, negligible).
- Full-attention layers: `num_key_value_heads: 2` (GQA), `head_dim: 256`.

**KV cache per token** (full-attention layers only), at the default f16:

```
12 layers x (K+V) x 2 kv_heads x 256 head_dim x 2 bytes  ≈ 24 KB / token
```

| Context | KV cache (f16) | Weights + KV + ~2 GB buffers |
| --- | --- | --- |
| 32K | 0.8 GB | ~52 GB |
| 64K | 1.6 GB | ~53 GB |
| 128K | 3.2 GB | ~54.5 GB |
| 256K (max) | 6.4 GB | ~58 GB |

The model supports the full **262,144 (256K)** tokens. On 64 GB, context is *not* the
binding constraint — even maxing it costs only ~6.4 GB of KV. Comfortable zone with the
sandbox running is **64K–128K**; full 256K is possible (~58 GB total) but leaves only
~6 GB headroom. Suggested cap (no quality cost):

```Modelfile
FROM hf.co/unsloth/Qwen3-Coder-Next-GGUF:UD-Q4_K_M
PARAMETER num_ctx 131072
```

### Default KV cache value (when NOT q8_0)

Ollama's KV cache type is controlled by `OLLAMA_KV_CACHE_TYPE`. On this host it is
**unset, which means the default: `f16`** (16-bit, **2 bytes per element**) — that is
the precision used in the ~24 KB/token figure above. The alternatives are opt-in and
require flash attention (`OLLAMA_FLASH_ATTENTION=1`):

| `OLLAMA_KV_CACHE_TYPE` | Bytes/elem | KV/token | 256K KV | Notes |
| --- | --- | --- | --- | --- |
| `f16` (**default**) | 2 | ~24 KB | ~6.4 GB | full precision, no quality loss |
| `q8_0` | ~1 | ~12 KB | ~3.2 GB | ~half memory, negligible quality loss |
| `q4_0` | ~0.5 | ~6 KB | ~1.6 GB | quarter memory, measurable quality loss |

So by default the KV cache is **f16** — the most memory-hungry but lossless option.
KV quantization (`q8_0`) is a lever to free memory for more context, but it was not
confirmed to be wired up for the `qwen3next` architecture and is not needed at the
recommended 128K cap on 64 GB.

## 6. What was pulled

```
ollama pull hf.co/unsloth/Qwen3-Coder-Next-GGUF:UD-Q4_K_M    # ~49.3 GB
```

Pulled as the `hf.co/...:UD-Q4_K_M` tag (does **not** overwrite the existing
`qwen3-coder-next:latest`). To make it a drop-in for danno/claurst without touching
`danno.toml` or `~/.claurst/settings.json`, alias it over the existing name **after
verifying it**:

```
ollama cp hf.co/unsloth/Qwen3-Coder-Next-GGUF:UD-Q4_K_M qwen3-coder-next:latest
```

> Status: pull kicked off 2026-06-25; verify with `ollama show` and a tool-calling
> probe before the `ollama cp` alias.
