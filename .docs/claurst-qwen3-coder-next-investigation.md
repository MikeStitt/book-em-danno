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

## 7. SWE-bench calibration of qwen3-coder-next (2026-07-07/08)

Follow-on: `danno bench --harness occ --only qwen3-coder-next-65k` on 3 SWE-bench
Verified instances (astropy-12907, django-16527, sympy-20590), a `qwen3-coder-next`
variant baked at `num_ctx 65536`. First run scored 3/6 with early-stops; the
diagnosis, fixes, and a review of the suite's design followed.

### 7.1 Why the early-stops happened (first run)

Two distinct mechanisms, plus environment gaps that amplified them:

- **Context exhaustion (astropy, sympy).** Both pinned at ~65,458–65,469 against the
  65,536 ceiling with negative ctx deltas (compaction churn); final turns were
  truncated no-tool-call preambles. This is *real* — a genuine window limit for that
  variant, not a bug.
- **Premature victory (django).** Stopped at 28 turns / 9,382 ctx declaring "the fix
  is already in place," having made **no edit**.
- **Environment gaps (amplifiers).** The model reflexively used `/testbed` (its
  training path — the real checkout is `/tmp/danno-swe/<instance_id>`): sympy logged
  4,808 `/testbed` refs and 2,970 "File not found". And it invoked `python …` but the
  `shell` image ships only `python3`, so self-verification calls silently failed.

### 7.2 Fixes applied — branch `fix/swebench-repo-path-and-python-shim`

Three commits (`ac4d03e`, `92be9ac`, `2f5704d`), gate green, in `swebench.py`:

1. **Real checkout path in the prompt** + an explicit "do NOT assume /testbed" — cut
   astropy `/testbed` refs 1075→51 and "File not found" 407→0 (sympy 4808→10, 2970→0).
2. **`python`→`python3` PATH shim** in `provision`. First attempt targeted
   `/usr/local/bin` (read-only: the `shell` VM runs as the unprivileged `agent` user);
   fixed to `~/.local/bin` (writable + first on PATH for login and non-login shells).
   `python: command not found` went 69→0.
3. **HF datasets-server retry** (`_fetch_rows_page`, linear backoff, 4xx fails fast) —
   a single transient 502 no longer aborts a 25-min run.

### 7.3 The three failures that remain are INDEPENDENT — 0/3 is not a fair signal

The clean re-run (all fixes active) still scored 0/3, for three *unrelated* reasons:

- **astropy → genuine context exhaustion** (65,532 against the 65,536 ceiling). Grader
  valid; a real capability/window result for this variant.
- **django → grader bug (false negative).** The model produced the *exact gold fix*
  (`has_add_permission` on `show_save_as_new`) in two separate runs and still graded
  fail. Cause: **SWE-bench node-id format mismatch** — see §7.4.
- **sympy → transport error, never graded.** Intermittent qwen3-coder-next
  malformed-tool-call: `OpenAI API error 500: XML syntax error … element <function>
  closed by </parameter>`. This is the **same 3B-active tool-call fragility** as §2
  (narrate-then-stop), here surfacing as a malformed emission rather than a missing one.

### 7.4 The grader bug (danno's, NOT upstream) — node-id format mismatch

`SwebenchTask.grade` runs a **uniform** `cd <repo> && python3 -m pytest <FAIL_TO_PASS +
PASS_TO_PASS ids>`. But SWE-bench stores each instance's ids in that repo's **native
runner format**, and bare pytest can only collect one of them:

| repo | node id as stored | pytest can collect? |
| --- | --- | --- |
| astropy | `astropy/modeling/tests/test_separable.py::test_separable[compound_model6-result6]` | ✅ proper `path::test[param]` |
| django | `test_submit_row_save_as_new_add_permission_required (admin_views.test_templatetags.AdminTemplateTagsTest.test_…)` | ❌ unittest `method (module.Class.method)` — needs `tests/runtests.py` + settings |
| sympy | `test_immutable` | ❌ bare name, no `path::` |

So django/sympy (and any non-pytest-path repo) **always grade FALSE even on the gold
patch**. This is a bug in **danno's own grader**, not upstream: the official SWE-bench
harness already carries per-repo eval specs (`MAP_REPO_VERSION_TO_SPECS`). Our env
fixes did NOT cause it (`grade()` is byte-for-byte unchanged, `workspace_dir`/
`_patch_path` refactor is behavior-preserving) — they *exposed* it by getting the model
far enough to emit a correct patch that then hit the broken grader.

### 7.5 Review — was diverging from the official SWE-bench harness right?

The DoR (`plan-claurst-swe-benchmarks.md`) is careful to say danno runs "*real
benchmark task content via danno's execution model*; we never claim an official …
score." Unbundling that one decision into three:

- **A. Execution via danno's `seed→run→grade` seam (not the official Docker harness)
  — RIGHT, and essentially forced.** danno's whole purpose is comparing *harnesses*
  head-to-head; the official harness has no seam to swap in claurst vs opencode vs occ.
- **B. Reimplementing grading with uniform pytest — WRONG.** The official per-repo
  eval specs are reusable *data*; we should have ported them instead of re-deriving a
  worse version. This is §7.4, and the fix belongs in danno.
- **C. Live proxy-only pip provisioning (not the official prebuilt images) —
  DEFENSIBLE but cost undersold.** Forced by the sandbox's proxy-only egress and
  security model, but it means we own all the per-instance provisioning flakiness the
  official images exist to remove.

**Recommendation:** keep SWE-bench as a small, clearly-caveated probe; port the
per-repo grade commands (or at minimum have `grade()` report `ungradeable` for
non-pytest id shapes rather than silently `False`), and lean on Aider Polyglot as the
primary local-model signal. A real fix needs per-repo test-command resolution in
`grade`, e.g. django → `python3 tests/runtests.py <label>`.

### 7.6 Correction of record — danno's scope is HYBRID local/cloud, NOT "65k local"

During review I asserted "danno's target is small local models on ~65k windows." **That
is unsupported — the `65k` came from *my* calibration variant, not any requirement.**
The docs state the opposite:

- Constitution (`.specify/memory/constitution.md`, "What this repository is"):
  "**hybrid local/cloud model runtimes** … cheap, high-volume agents run **locally**
  (tuned for a MacBook Pro) while **cloud models run the high-stakes agents**."
- Config defaults span **`context_budget = 32000`** (default local agent,
  `danno.toml.example:24`) **up to `1000000`** (a cloud agent, line 49).

So SWE-bench's context weight is a non-issue for the cloud/Claude slice and only
context-binds the small-local slice. The honest, narrow claim: SWE-bench's signal is
**uneven across danno's matrix** (fine for cloud, noisy for small-local), not that
danno "targets 65k." See [[bench-cloud-auth-and-occ-local-routing]] and the grader
memory [[swebench-grader-nodeid-mismatch]].
