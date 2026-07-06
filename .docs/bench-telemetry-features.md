# `danno bench` telemetry — feature catalog

A menu of metrics/artifacts we *could* record per bench permutation, grounded in what
danno's code can already do. Nothing here is committed work — it's a prioritization
surface. A "permutation" = one `(agent × model × task)` run = one `BenchVerdict` row.

**Portability rule:** nothing Apple-specific. The dev host is a Mac, but real
benches run on Linux/NVIDIA. Every sampler below is chosen to work there:
`psutil`/`/proc` for CPU+mem, `nvidia-smi` for GPU, Ollama `/api/*` for model-server
stats (all cross-platform; AMD would swap `nvidia-smi`→`rocm-smi`).

**Effort legend:** ✅ already emitted · 🟢 small (a seam already exists) ·
🟡 medium (new sampler/parser, no new infra) · 🔴 large (new subsystem).

---

## 0. What bench records **today** (baseline)

`bench.json` per row (`suites/bench.py:_write_results`, `suites/base.py:BenchVerdict`):

| field | source | granularity |
|---|---|---|
| `passed` | `task.grade()` (pytest exit 0) | bool |
| `verdict` | `oracle.classify_turn` | pass/stall/refusal/error/hallucinated-tool |
| `tool_calls` | `turn.tool_call_count` | **count only** — no per-call timing/errors |
| `tokens` | `turn.tokens` | **total only**; `0` for occ (stream-json) & local claurst |
| `cost` | `turn.cost` | per-turn total; `0` for local models |
| `latency_s` | `time.monotonic()` around the turn | **end-to-end only** |
| `error` | `turn.error_summary` | string |

Everything below is what's *missing* or *coarser than useful*.

---

## 1. Token metrics

| # | feature | captures | effort | hook |
|---|---|---|---|---|
| 1.1 | **prompt / completion split** | separate input vs output tokens (not just sum) | 🟢 | OpenAI `/v1` responses already carry `usage.prompt_tokens`/`completion_tokens`; today we sum them. Parse both, don't add. `driver.py` `*Turn.tokens`. |
| 1.2 | **cached / reused tokens** | prompt-cache hits (cloud) | 🟢 | Claude `result.usage` has `cache_read_input_tokens`; already in the payload, just unread. |
| 1.3 | **occ token totals** | occ currently reports `0` | 🟡 | occ's `stream-json` emits no totals; its `json` mode does — either switch occ's bench mode or derive from wire capture (§3). |
| 1.4 | **per-round token deltas** | tokens added each tool-call round | 🟡 | Needs per-request accounting → falls out of §3 capture or §6 context profile. |
| 1.5 | **tokens/sec (throughput)** | completion_tokens ÷ generation time | 🟡 | Needs 1.1 + a generation-duration signal (§2.3 or §4). The headline "is this model fast enough" number. |

## 2. Timing / elapsed

| # | feature | captures | effort | hook |
|---|---|---|---|---|
| 2.1 | end-to-end latency | ✅ have it | ✅ | `run_bench_task` wall clock. |
| 2.2 | **time-to-first-token (TTFT)** | prefill latency — huge for slow local models | 🟡 | Requires reading the streamed response; occ/opencode local path is *non-streaming* (`CLAUDE_CODE_STREAMING=0`), so TTFT there = whole-response time. Meaningful only on streaming paths; note the caveat. |
| 2.3 | **per-request round-trip time** | latency of each `/v1` call within a turn | 🟡 | The capture proxy (§3) sees request→response timestamps per call — subtract them. Gives the latency profile across a multi-round turn. |
| 2.4 | **per-tool-call duration** | wall time each tool ran | 🔶 | Agents don't emit tool timing; would need agent-side instrumentation. Low ROI. |
| 2.5 | **model load time** | cold-start / model-swap stall | 🟢 | Ollama `load_duration` (native `/api`) or observe first-request spike via §5 `/api/ps`. |

## 3. Wire transcripts — "what was sent to / from the AI" ⭐

This is the big one, and it **mostly already exists** — it's just not wired into bench.

| # | feature | captures | effort | hook |
|---|---|---|---|---|
| 3.1 | **full request/response bodies per permutation** | system prompt, message history, tool schemas, every completion — as JSONL | 🟢 | `capture/proxy.py` already records exactly this (full bodies, secrets redacted) to `<out>/captures/<backend>.jsonl`. It's **validate-only** today (`run.py:_setup_capture`); bench has no `--capture`. Wiring = add capture opt to `BenchOptions`/CLI + call `plan_capture`/`captures_running` around the run. occ/claurst already forward through the capture proxy under `--capture` via the relay. |
| 3.2 | **per-permutation namespacing** | one transcript file per `(agent,model,task)` | 🟢 | Extend the capture filename with the permutation key so rows don't collide. |
| 3.3 | **HTTP round-trip trace** | connection lifecycle, upstream status, byte counts (not bodies) | ✅ | Relay already does this via `DANNO_RELAY_LOG` env (`driver.py:_OLLAMA_RELAY_SOURCE`); claurst+occ only. Good for diagnosing hangs; §3.1 supersedes it for content. |
| 3.4 | **prompt/response artifacts saved beside `bench.json`** | human-readable `.md`/`.txt` dump per turn | 🟡 | Post-process the §3.1 JSONL into readable transcripts for review. |

**Note:** because agents use the OpenAI `/v1` path, the capture proxy's response
bodies contain `usage` → §3 is also the cheapest route to §1.1/1.3/1.4/2.3.

## 4. Model-server metrics (Ollama, in-band)

Ollama's *native* `/api/generate|chat` returns `prompt_eval_count`, `eval_count`,
`prompt_eval_duration`, `eval_duration`, `load_duration`, `total_duration`.

| # | feature | captures | effort | hook |
|---|---|---|---|---|
| 4.1 | native eval counts/durations | authoritative server-side token + timing | 🔴 | Agents dial `/v1`, **not** `/api`, so these fields never come back in-band. Getting them means either a relay that translates/annotates, or out-of-band polling (§5). Flag as high-value-but-costly. |

## 5. Host / server resource sampling (CPU · GPU · mem) — non-mac

Sample on an **interval** during each permutation → a **profile over time**, not one
number. A background sampler writes `<out>/samples/<permutation>.jsonl` with a
timestamp per tick; align to turn start/end.

| # | feature | captures | effort | hook |
|---|---|---|---|---|
| 5.1 | **CPU utilization + load** | host CPU % during inference | 🟡 | `psutil.cpu_percent` (cross-platform) or `/proc/stat` (Linux). Sample where Ollama runs (the host), not just the VM. |
| 5.2 | **memory (RSS / used / avail)** | RAM pressure during a run | 🟡 | `psutil.virtual_process` / `/proc/meminfo`. |
| 5.3 | **GPU utilization + VRAM** | GPU %, mem used/total, temp, power | 🟡 | `nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader` on a timer. NVIDIA/Linux — **not** mac. AMD → `rocm-smi`. |
| 5.4 | **model-attributed VRAM** | VRAM *this model* holds + is it resident | 🟢 | Ollama `/api/ps` → `size_vram`, `expires_at` per running model. Cross-platform, no GPU vendor lock-in. Also detects mid-bench model evictions/swaps. |
| 5.5 | **peak vs mean rollups** | summarize the profile into `bench.json` | 🟢 | Reduce the sample series to peak/mean/p95 columns per row; keep the raw series in `samples/`. |

**Design note:** a pluggable `Sampler` interface (CPU, mem, GPU backends) keeps it
portable — `nvidia-smi` present → GPU rows; absent → skip, no failure. Never assume
a vendor.

## 6. Context profiling over time ⭐

The interesting question: how full did the context window get, and how did it grow?

| # | feature | captures | effort | hook |
|---|---|---|---|---|
| 6.1 | **context occupancy per request** | prompt_tokens each `/v1` call → the fill level | 🟢 | Parse `usage.prompt_tokens` per request from §3 capture. |
| 6.2 | **growth curve across a turn** | occupancy vs tool-call round (the profile) | 🟡 | Series of 6.1 across the turn's requests → "context grew 4k→38k over 12 rounds." |
| 6.3 | **headroom vs the real ceiling** | how close to the model's loaded `num_ctx` | 🟡 | `context_budget` in danno.toml is only opencode's **client-side** trim belief; `/v1` ignores `num_ctx` and loads the model's **full** context. True ceiling = model's loaded ctx (`/api/show`/`/api/ps`). Compare 6.1 against *that*, not the budget. |
| 6.4 | **compaction / truncation events** | when history got summarized/dropped | 🔶 | Detect a prompt_tokens drop between consecutive rounds (§6.2) or agent compaction markers. Explains sudden context resets. |

## 7. Run provenance (repro / comparability)

Cheap metadata that makes cross-run comparison honest.

| # | feature | captures | effort | hook |
|---|---|---|---|---|
| 7.1 | resolved model id + digest | exact model bytes (`/api/tags` digest) | 🟢 | Pin *which* build of a tag ran. |
| 7.2 | model params | quantization, `num_ctx`, param count | 🟢 | `/api/show`. |
| 7.3 | agent + fork versions | occ SHA, claurst/opencode versions, danno commit | 🟢 | Already known at provision time; just record it. |
| 7.4 | host descriptor | CPU model, core count, GPU model, driver, total VRAM | 🟢 | One-shot at run start (`nvidia-smi -q`, `/proc/cpuinfo`, `psutil`). |

---

## Suggested phasing

1. **Wire `--capture` into bench (§3.1–3.2)** — unlocks §1.1, §1.3, §1.4, §2.3, §6.1
   almost for free, since the proxy already records `/v1` bodies with `usage`. Highest
   leverage, smallest new code (the subsystem exists).
2. **Token split + throughput (§1.1, §1.5) + per-request timing (§2.3)** — parse what
   capture now gives us into `bench.json` columns.
3. **Resource sampler (§5)** — pluggable CPU/mem/GPU profile, peak/mean rollups. The
   `nvidia-smi` + `/api/ps` combo is the non-mac GPU story.
4. **Context profile (§6.2–6.3)** — the growth curve + real-ceiling headroom.
5. **Provenance (§7)** — bolt on anytime; makes every prior number comparable.
6. **Native Ollama eval metrics (§4)** — defer; needs a translating relay or accept
   §5's out-of-band approximation.

## Cross-cutting

- **Per-permutation keys.** Every artifact (`captures/`, `samples/`) namespaced by
  `(agent,model,task)` so nothing collides across the matrix.
- **Overhead honesty.** Sampling and capture perturb timing slightly — record the
  sampler interval so latency numbers stay interpretable; keep capture opt-in.
- **Secrets.** The capture proxy already redacts `Authorization`/`x-api-key`; any new
  transcript dump (§3.4) must inherit that redaction — never persist a raw key.
- **`bench.json` stays the index.** Big series live in sidecar files; `bench.json`
  gets summary columns (totals, peak/mean) + relative paths to the raw artifacts.
