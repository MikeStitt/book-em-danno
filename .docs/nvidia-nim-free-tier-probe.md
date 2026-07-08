# NVIDIA NIM free-tier probe — setup, findings, and cost/turn-cap analysis

**Date:** 2026-07-08 · **Status:** probe *set up but not run* (pivoted to the
qwen3-coder-next-65k local sweep before executing). This doc archives everything so the
probe can be run later without re-deriving it.

## Goal

Run three confirmed-callable small NVIDIA NIM cloud models across three harnesses
(occ / claurst / opencode) on **one easy aider task** (`python/proverb`) to observe:

1. **What prompts cross the wire** in claurst vs opencode (`--capture` JSONL).
2. **Per-task timing / latency** (`bench.json` `latency_s`, recorded regardless of capture).
3. **Whether NVIDIA cloud works AT ALL** through each harness (not just raw `curl`).
4. **Whether the NVIDIA free tier is too intermittent** (500 / 404 / timeout) to bench.

`python/proverb` is the easiest of the three python aider exercises we use (build a
string from a list; no roster class like grade-school, no matrix logic like transpose),
so it isolates the harness×model tool-calling plumbing — the binding constraint on
trivial coding tasks.

## Where the config lives

| file | role |
|---|---|
| `book-em-danno/danno.toml.example` | committed example — the 3 small models added to the `[models]` catalog with honest per-model status comments |
| `bench2/danno.nvidia-probe.toml` | archived probe config: `[backends.danno-nvidia]` + the 3 working models + `profile="cloud-only"` |
| `bench2/benchmarks.nvidia-probe.toml` | archived probe suite: `harnesses=[occ,claurst,opencode]`, aider `python/proverb` only, swebench disabled |
| `bench2/danno.toml` / `bench2/benchmarks.toml` | **the live qwen3-coder-next-65k sweep** (restored) — *not* the probe |

`bench2/` is **not** a git repo, so the probe `.toml`s are preserved on disk as
`*.nvidia-probe.toml` (matching the existing `benchmarks.swe-only.toml` convention),
not committed. To run the probe: `cp danno.nvidia-probe.toml danno.toml` +
`cp benchmarks.nvidia-probe.toml benchmarks.toml` in `bench2/`, then:

```bash
danno bench -C /Users/mikestitt/projects/bench2 \
  --harness occ --harness claurst --harness opencode \
  --capture --sample \
  --out /Users/mikestitt/projects/bench2/.danno-bench/nvidia-probe
```

`NVIDIA_API_KEY` is auto-forwarded from the host env into each harness's chmod-600
env-file (occ emits `OPENAI_BASE_URL`+`OPENAI_API_KEY`; claurst dials `nvidia/<tag>`
directly through the sandbox `HTTPS_PROXY`; opencode needs `.opencode/opencode.jsonc`
seeded). The secret never lands on a command line.

## Finding 1 — the free tier IS heavily gated / intermittent (partial answer to goal 3+4)

Live smoke tests against `https://integrate.api.nvidia.com/v1` with a valid free-tier
key (HTTP 200, catalog lists ~121 models). Of the small models probed, **only 3 of ~11
were actually callable**:

**Working (HTTP 200 + real completion):**
- `meta/llama-3.1-8b-instruct` — 0.8s, **clean OpenAI `tool_calls`**
- `mistralai/ministral-14b-instruct-2512` — 1.3s, **clean `tool_calls`**
- `nvidia/nvidia-nemotron-nano-9b-v2` — 2.3s, reasoning model; **emits `<TOOLCALL>[…]`
  as raw text in `content`, NOT a structured `tool_calls` array** (reply text lands in
  `reasoning_content`; `content="\n\nOK\n"`). Will stress each harness's tool-parse path.

**Dead on this free account:**
- `mistralai/mistral-nemotron` — HTTP **500** "Inference connection error" + 60s timeouts
  (was ranked pick #1; NVIDIA's own featured agentic model, but down/unreachable)
- `mistralai/codestral-22b-instruct-v0.1` — HTTP **404** "not found for account"
  (ranked #2; in the catalog *list* but **not entitled** to this account)
- `nvidia/llama-3.1-nemotron-nano-8b-v1`, `qwen/qwen3-next-80b-a3b-instruct` — timeout
- `nvidia/mistral-nemo-minitron-8b-8k-instruct`, `mistralai/mistral-7b-instruct-v0.3`,
  `deepseek-ai/deepseek-coder-6.7b-instruct`, `ibm/granite-8b-code-instruct` — 404 gated

**Takeaway:** catalog presence ≠ callable. Two of three originally-ranked picks were
non-functional. The free tier is usable for a probe only if you *first* smoke-test each
model; it is **not reliable enough to schedule an unattended benchmark sweep against**
without a fallback/retry story.

## Finding 2 — the turn cap (`--max-turns`) is harness-level and UNEQUAL

There is **no `danno bench --max-turns` flag**; the cap is hardcoded per harness driver:

| harness | cap in the bench path | mechanism |
|---|---|---|
| **occ** | **30** | `occ_run` always appends `--max-turns 30` (`driver.py:1100`, `OCC_DEFAULT_MAX_TURNS`); stop event reports `reason="max_turns"`/`"max_recursion"` |
| **claurst** | **~10** (implicit) | `claurst_run` passes **no** `--max-turns` (`driver.py:901–909`) → rides claurst's own built-in default of 10 |
| **opencode** | **none** | `opencode_run` passes no step/turn cap; `opencode run` loops to completion — the prior local proverb cell hit **383 tool_calls / 2314s** |

Two consequences:
- **Fairness bug:** occ (30) vs claurst (10) vs opencode (uncapped) is not an
  apples-to-apples comparison. A single normalized `--max-turns N` bench flag that drives
  all three would fix it. *(Follow-up worth doing before any comparative cost claim.)*
- **Runaway risk is opencode-only.** occ/claurst are bounded even when nemotron's
  `<TOOLCALL>`-in-content defeats tool parsing (failed parses still burn turns → hit the
  30/10 ceiling). **opencode + nemotron is the one genuinely runaway cell.**

## Finding 3 — paid-tier cost estimate

danno logs `cost: 0.0` (no price table wired for the nvidia backend) and the prior
calibration logged `tokens: 0` (local Ollama reports no usage), so cost is *estimated*
externally from tool-call volume, not measured.

**Volume basis** (cost ≈ `turns × system_prompt + accumulated_history`):
- opencode (~12K-token system+tools prompt): well-behaved ~15–40 turns → ~0.3–0.6M tokens/cell
- claurst (mid prompt): ~0.2–0.4M/cell
- occ (one-sentence prompt): ~0.15–0.25M/cell
- Well-behaved 9-cell total ≈ **2–4M tokens**; a single proverb-style loop adds **30–80M**.

**Two NVIDIA paid pricing models:**
- **Per-token** (partner/hosted rates for these small models, ~$0.10–0.20 blended/1M):
  well-behaved **$0.20–$0.90**; with a looper **~$3–$15**.
- **Self-hosted NIM** (the more common NVIDIA paid path — GPU-hour via AI Enterprise,
  ~$1/GPU-hr cloud, not per token): cost = wall-clock; 9 small-model cells serial ≈
  20–90 min → **~$0.30–$1.50**.

**Bottom line:** well under $1 if runs behave; budget up to ~$10–15 as the loop-blowup
ceiling — and that tail lives entirely in the *opencode* cells (occ/claurst are capped).
These are 8–14B models, so even the pathological case is cheap; the risk is token
*volume* from a tool-parse loop (nemotron the prime suspect), not per-token price.

## Related memory / prior art

- `danno-benchmarks-the-triple` — every failure is attributed to a leg of harness×model×sandbox.
- `swebench-grader-nodeid-mismatch`, `bench-cloud-auth-and-occ-local-routing`,
  `occ-fork-long-slow-loops` — harness identity + cloud-auth wiring.
