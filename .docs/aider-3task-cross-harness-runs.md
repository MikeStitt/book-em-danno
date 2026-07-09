# Aider 3-task cross-harness / cross-model runs — results and conditions

**Date compiled:** 2026-07-08 · **Status:** observational record (no capability
claim — see caveats). Documents every run we have of the same three Aider-polyglot
Python exercises, the exact conditions each ran under, and the resolution of the
long-standing "which Claude model?" question.

## The tasks

Three self-graded Aider-polyglot Python exercises, run with `isolation = "shared"`
(each exercise gets a fresh workspace reset; the exercises are self-contained):

- `python/grade-school` — a roster class (add students to grades, sorted queries)
- `python/proverb` — build a cumulative string from a list ("For want of a nail …")
- `python/transpose` — transpose ragged text (matrix-ish padding logic)

Grading is danno's Aider suite verdict (the exercise's own test file), so
`passed` is an objective pass/fail, not a judge.

## Full results — pass flags, per-row totals, sorted by total latency

Every comparison row below passed all three tasks (`3/3`); the `✓` marks the
per-cell pass. Cells show `tool_calls / latency_s`. Rows are sorted by **total
latency, ascending** (fastest first).

| # | harness | date | model | grade-school | proverb | transpose | **Σ tc** | **Σ latency** | pass |
|---|---|---|---|---|---|---|---|---|---|
| 1 | claude | 07-05 | `claude-opus-4-8[1m]` (default) | ✓ 6 / 37.2s | ✓ 6 / 28.2s | ✓ 6 / 36.0s | **18** | **101.4s** | 3/3 |
| 2 | opencode | 07-08 | qwen3-coder-next-65k *(wire run)* | ✓ 8 / 143.3s | ✓ 4 / 38.2s | ✓ 7 / 45.1s | **19** | **226.6s** | 3/3 |
| 3 | opencode | 07-05 | qwen3-coder-next:latest | ✓ 6 / 58.9s | ✓ 7 / 68.2s | ✓ 6 / 103.6s | **19** | **230.7s** | 3/3 |
| 4 | opencode | 07-08 | qwen3-coder-next-65k *(qwen-aider run)* | ✓ 6 / 151.2s | ✓ 7 / 42.9s | ✓ 6 / 38.1s | **19** | **232.2s** | 3/3 |
| 5 | claurst | 07-05 | qwen3-coder-next:latest | ✓ 6 / 62.2s | ✓ 6 / 54.9s | ✓ 8 / 141.4s | **20** | **258.5s** | 3/3 |
| 6 | claurst | 07-08 | qwen3-coder-next-65k *(wire run)* | ✓ 10 / 151.1s | ✓ 6 / 56.1s | ✓ 8 / 83.9s | **24** | **291.1s** | 3/3 |
| 7 | occ | 07-05 | qwen3-coder-next:latest | ✓ 50 / 596.1s | ✓ 15 / 107.6s | ✓ 10 / 116.7s | **75** | **820.4s** | 3/3 |
| 8 | *(unrecorded)* | 07-06 | **qwen3.6:27b-q4_K_M** *(different model)* | ✓ 9 / 267.6s | ✓ 7 / 206.1s | ✓ 7 / 352.5s | **23** | **826.2s** | 3/3 |
| 9 | occ | 07-07 | qwen3-coder-next-65k *(calibration)* | ✓ 16 / 128.2s | ✓ 383 / 2313.6s | ✓ 100 / 764.1s | **499** | **3205.9s** | 3/3 |

### What the sort shows

- **cloud `claude-opus-4-8[1m]` is the floor** (101s, 18 tc) — a capable cloud model
  on a trivial suite.
- **opencode and claurst on qwen cluster tightly** (227–291s, 19–24 tc) regardless of
  date or `:latest`-vs-`-65k` variant. The two opencode-on-65k runs (rows 2 and 4)
  landing at 226.6s vs 232.2s is the useful signal: the 65k model is run-to-run stable.
- **occ is the tail, both times.** Even its well-behaved run (row 7) is 820s / 75 tc;
  its calibration run (row 9) blew up to **3206s / 499 tc**, driven almost entirely by a
  single proverb cell at **383 tool_calls**. This tool-call inflation is an **occ-leg
  behavior**, not model nondeterminism (opencode/claurst on the same model never loop).
- **Row 8 is a different model** — see below.

### Rows that are NOT in the table (and why)

- **gemma3:27b "fast" cells (07-05), all harnesses** — every cell failed at ~1s with
  0 tool calls. `gemma3:27b` lacks tool-calling support, so the harness can't drive the
  edit loop at all. These are startup failures, not capability results, and are excluded.
  (Provenance: `.../claurst-vtest/.danno-bench/{occ,claurst,opencode}-fast/`.)

## The "which Claude model?" question — resolved

The 07-05 claude row (row 1) was recorded by danno as `"(default model)"`: the
`danno bench` claude path does **not** pin a model, so the row inherited whatever the
in-sandbox `claude` CLI defaulted to, and danno logged the placeholder rather than a
concrete id. `--capture` cannot help here — it does not (and by design cannot) record
the claude row, because claude talks straight to `api.anthropic.com` and danno has no
`base_url` proxy lever for it (`suites/bench.py:243` warns exactly this).

**Resolution (2026-07-08):** we ran the `claude` CLI headless *through danno's own
launch path* and read the authoritative `model` field from Claude Code's
`--output-format json` system-init event (and cross-checked by asking the model
directly). Both agree:

```
model            = claude-opus-4-8[1m]      # Opus 4.8, 1M-token context variant
contextWindow    = 1000000
maxOutputTokens  = 64000
claude_code ver  = 2.1.162
apiKeySource     = apiKeyHelper
```

Command used (token supplied via a chmod-600 `--env-file`, never on the command line):

```bash
cd <a claude sandbox workspace>
danno sandbox start --harness claude --apply -- \
  -p "state your exact model id" --output-format json --verbose
# (in this non-TTY shell the -it launch failed, so the same call was re-issued as a
#  plain `docker sandbox exec --env-file <chmod-600> <sandbox> claude -p … --output-format json`)
```

**Caveat on the historical row:** this confirms the *current* (2026-07-08) install
default is `claude-opus-4-8[1m]`. The 07-05 row almost certainly used the same default,
but danno did not record it at the time, so we cannot prove the 07-05 default was
byte-identical. The per-task cost logged on 07-05 (transpose $0.259, grade-school
$0.200, proverb $0.155) is consistent with an Opus-tier model, corroborating this.

Going forward, the fix is to **pin the claude model explicitly** so the id is recorded
— see the companion change adding an `inert` backend + a per-model claude sweep
(`danno bench --harness claude` across `haiku`/`sonnet`/`opus`/`fable`).

## Follow-up: the claude 4-model sweep (2026-07-08)

With the `inert`-backend sweep shipped, we ran `danno bench --harness claude` over all
four declared models on the same three tasks — the first run where each Claude model is
a **distinct, recorded row** instead of the single `(default model)` line above. All
**12/12 cells passed**. Cells show `tool_calls / latency_s`; sorted by total latency.

| model | grade-school | proverb | transpose | **Σ tc** | **Σ latency** | **Σ cost** | pass |
|---|---|---|---|---|---|---|---|
| `claude-sonnet-4-6` | 6 / 21.1s | 6 / 19.2s | 6 / 21.1s | **18** | **61.4s** | $0.286 | 3/3 |
| `claude-opus-4-8` | 5 / 23.6s | 7 / 40.7s | 6 / 41.8s | **18** | **106.1s** | $0.490 | 3/3 |
| `claude-fable-5` | 6 / 36.5s | 6 / 32.7s | 8 / 53.5s | **20** | **122.7s** | $0.595 | 3/3 |
| `claude-haiku-4-5` | 6 / 27.7s | 6 / 19.4s | 7 / 76.0s | **19** | **123.1s** | $0.234 | 3/3 |

Total run cost **$1.60** for the 12 cells. Notes:

- **All four models are entitled** on this account and solve every task — no gated/404
  model (contrast the NVIDIA free-tier probe, where most small models were dead).
- **`sonnet` was fastest** (61s, and the cleanest — 6 tool_calls every cell). **`haiku`
  was cheapest** ($0.234) but its total was dragged up by a single 76s transpose cell;
  **`opus` sits between** on latency but costs 2× haiku. **`fable` was the slowest and
  priciest** here — on a trivial suite that mostly measures loop overhead, not a
  capability verdict.
- **These are the same-day, same-sandbox conditions** as row 1's resolution above, so
  the `claude-opus-4-8` numbers here (106s / 18 tc) are directly comparable to the 07-05
  default row (101s / 18 tc) — consistent, reinforcing that the 07-05 default was Opus.
- **n = 1 per cell.** The transpose spread (haiku 76s vs sonnet 21s) is a single-sample
  latency, not a stable per-model figure. Cost, by contrast, is real and repeatable.
- **Provenance:** `book-em-danno/.danno-bench/2026-07-08T21-46-02/bench.json` (written
  relative to the CWD the sweep ran from — the repo root — not the `-C` config dir).
  Config: `bench2/claude/danno.toml` (the four inert models) + `bench2/benchmarks.toml`.

## Runtime change (2026-07-08): pre-warm + load-timing plot

The rows above were all recorded **before** danno pre-warmed the local model, so an
unknown amount of each qwen row's latency is Ollama **model-load time** leaking into the
first timed cell — a hidden, uncontrolled variable (worsened by mixing `:latest` and
`-65k`, whose different runners evict each other). Two changes close this going forward
(branch `bench-prewarm-and-load-timing`):

- **Pre-warm is default-on** (`danno bench`, opt out with `--no-warm`). Before the timed
  matrix, danno loads each unique local `ollama/<tag>` once via
  `/v1/chat/completions` — the **same transport the harnesses use**, so it loads the exact
  runner they reuse (a small-`num_ctx` `/api/generate` warm-up would load a *different*
  runner and the harness would still pay the cold load). Cloud/claude refs are skipped;
  a refused warm-up is non-fatal (the bench still runs, cell #1 just pays the load, as
  before). The cold-start posture is recorded in `provenance.json` under `warmup` and
  summarized in the report, e.g. *"1 pre-warmed — 0 already resident, 1 cold-loaded;
  slowest load 41.3s"*. With `keep_alive` set to hours (this host), a warmed sweep takes
  **zero** cold loads on timed cells; the single load is absorbed by pre-warm and reported
  separately.
- **A load-timing plot** in `report.html` (inline SVG, `--capture` runs only): per cell,
  first-call `ttft_s` vs steady-state `rtt_mean_s`. A **red** first-call bar flags a load
  that leaked into a timed cell (>1.5× steady **and** >1s absolute) — the model-faithful
  anomaly signal. (Note: `resource.model_load_s` is a weak detector in the hours-`keep_alive`
  regime — it's name-agnostic and reads `None` whenever anything is resident at tick 0 — so
  the `ttft`-vs-`rtt` chart, not `model_load_s`, is the signal to read.)

So a **re-run of these nine rows under the new default would remove the model-load
confound**; treat the qwen latencies above as *including* a possible one-time load hit.

## Conditions common to all runs

- **The "triple".** danno's unit-of-test is **harness × (model+config) × sandbox**, not
  the model in isolation. Every difference above is attributable to a leg, and the occ
  tail is a *harness*-leg fact. See memory `danno-benchmarks-the-triple`.
- **Sandbox.** Docker `sandbox` VMs (one per cell), egress **proxy-only** (direct egress
  blocked; local Ollama reachable only via `--allow-host localhost:11434`). Local models
  served by host Ollama at `host.docker.internal:11434`; cloud claude carries its own
  OAuth token injected into a chmod-600 env-file at launch.
- **`--max-turns` is harness-level and UNEQUAL** (there is no bench-level flag):
  - **occ** = 30 (`occ_run` always appends `--max-turns 30`)
  - **claurst** ≈ 10 (rides its own built-in default; danno passes none)
  - **opencode** = uncapped (`opencode run` loops to completion)

  So the runaway tail is structurally **opencode's** on pathological cells, while occ's
  large-but-bounded counts sit under its 30-turn ceiling. This inequality is itself a
  fairness gap; a normalized `danno bench --max-turns N` would close it. See memory
  `nvidia-nim-free-tier-probe` (Finding 2).
- **Cost/token accounting.** Local Ollama reports no usage, so danno logs `tokens: 0`
  and `cost: 0.0` for every local cell — the tool_calls / latency columns are the only
  cross-harness signal for the qwen rows. Only the cloud claude row carries real cost.

## Provenance (where each row's data lives)

| # | row | path (bench.json) |
|---|---|---|
| 1 | claude 07-05 | `book-em-danno/scratch/claurst-vtest/.danno-bench/claude-ref/` |
| 2 | opencode 07-08 wire | `bench2/.danno-bench/wire-opencode-claurst/opencode/` |
| 3 | opencode 07-05 | `book-em-danno/scratch/claurst-vtest/.danno-bench/opencode-qwen/` |
| 4 | opencode 07-08 qwen-aider | `bench2/.danno-bench/qwen-aider/` |
| 5 | claurst 07-05 | `book-em-danno/scratch/claurst-vtest/.danno-bench/claurst-qwen/` |
| 6 | claurst 07-08 wire | `bench2/.danno-bench/wire-opencode-claurst/claurst/` |
| 7 | occ 07-05 | `book-em-danno/scratch/claurst-vtest/.danno-bench/occ-slow/` |
| 8 | 07-06 qwen3.6:27b | `bench/.danno-bench/2026-07-06T14-35-34/` |
| 9 | occ 07-07 calibration | `bench2/.danno-bench/calibration/` |

Notes: `bench2/` and `bench/` are **not** git repos (working scratch dirs), so these
artifacts are not committed — this doc is the durable record. The 07-06 row's harness
was not recorded (`provenance.json` `harness: None`); it is a **different model**
(`qwen3.6:27b-q4_K_M`, a general 27B, *not* qwen3-coder-next) and is kept out of the
harness comparison for that reason.

## Caveats / how to read this

- **Not a capability leaderboard.** These are three trivial exercises; all capable
  configs pass. The signal is *cost of the harness loop* (tool_calls, latency), not
  whether the model "can" code.
- **occ tool-call inflation** is the dominant latency driver and is a harness-leg fact
  reproducible across runs; it is not the model being nondeterministic.
- **Dates span 07-05 → 07-08** and mix `qwen3-coder-next:latest` with the `-65k`
  Modelfile variant; treat cross-date rows as directional, not controlled A/Bs.
- **These rows predate pre-warm** (see "Runtime change" above), so a qwen latency may
  embed a one-time Ollama model-load hit that a warmed re-run would remove.
