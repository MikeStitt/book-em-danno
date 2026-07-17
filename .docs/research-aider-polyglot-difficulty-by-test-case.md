# Research: public-domain data on Aider Polyglot per-test-case difficulty

**Date:** 2026-07-17
**Question:** Is there a published dataset that indicates failure/pass rates for the
*individual* Aider Polyglot exercises across a wide range of models — something danno
could use to order the 225 exercises easiest → hardest?
**Short answer:** **No.** No public source reports pass/fail at per-exercise
granularity across models. Every public leaderboard and data release stops at
*per-model aggregate* scores. The only per-exercise difficulty signal that has ever
been computed — Aider's own benchmark-selection filter — was published only as three
bucket totals, never as a per-exercise table.

---

## Definition of "difficulty" used here

"Easy" and "hard" are **relative to observed pass rates across the current
(July 2026) model field**, not to an exercise's nominal Benchmark Exercise reputation. Formally,
for exercise *e*:

> difficulty(*e*) = 1 − mean over models *m* of pass_rate(*m*, *e*)

A low average pass rate ⇒ hard. This is the only definition grounded in data rather
than intuition — and, as the findings below show, it is **not** something the public
record lets us compute per exercise.

---

## What the benchmark actually is

The Aider Polyglot benchmark is **225 Benchmark Exercises** across six languages
(C++, Go, Java, JavaScript, Python, Rust). Each model gets **two attempts** per
exercise (test output from attempt 1 is fed back before attempt 2); the headline
metric is `pass_rate_2` (fraction of exercises with all tests passing after the
second attempt). Source code lives in `Aider-AI/polyglot-benchmark`; danno vendors it
as the `aider_polyglot` suite.

### The 225 are already difficulty-filtered — this is the key fact

Aider did not pick 225 problems at random. Per Aider's launch write-up
(2024-12-21), they:

1. Started from a pool of **697** Benchmark Exercise problems across the six languages.
2. Ran **7 of the strongest code-editing models** of late 2024 (e.g. o1, Claude 3.5
   Sonnet, DeepSeek V3) over **all 697**.
3. Bucketed by how many of the 7 solved each problem:
   - **258** solved by **all 7** → dropped as *too easy*.
   - **66** solved by **none** → dropped as *too hard*.
   - **225** solved by **3 or fewer** (i.e. **1–3** of 7, since the 0-solved set was
     removed as too hard) → **kept as the benchmark.**

So a per-exercise difficulty score — "how many of 7 models solved it," on a 1–3 scale
within the kept set — **was computed** and is the whole basis of the benchmark. But
Aider released only the three bucket *totals* (258 / 66 / 225). The per-exercise
"solved by N of 7" table has never been published.

**Consequence for danno's ordering.** Every one of the 225 is, by construction,
a problem that **≥4 of 7 strong 2024 models failed** in that specific language.
A nominally-trivial exercise (`grade-school`, `gigasecond`) is present *only in the
language variants where most models failed it*. Its "easy Benchmark Exercise reputation" is
therefore not evidence it is easy here — if anything, its inclusion is evidence it
tripped up the field. Any ranking built from generic Benchmark Exercise difficulty is measuring
difficulty **before** the filter that already removed all the genuinely easy cases,
and cannot be defended as an easiest → hardest ordering of *these* 225.

---

## What is public, by granularity

| Granularity | Public? | Where |
|---|---|---|
| Per-model aggregate `pass_rate_2`, edit-format %, cost | **Yes** | Aider leaderboard; Epoch AI (downloadable CSV, Apache-2.0); llm-stats.com |
| Per-**language** pass rate for a given model | **Sometimes** | Ad-hoc in blog posts / papers (e.g. a Qwen3.5-9B run: 19.1% overall, 12.8% Go … 26.9% C++) |
| Per-**exercise** pass/fail, one model | Generated locally, **not** aggregated/published | Aider's harness writes `.aider.results.json` per exercise (`tests_outcomes` field); only exists if *you* run it |
| Per-**exercise** across many models (what we want) | **No public source** | — |
| Aider's own per-exercise "solved by N of 7" filter scores | **No** — only the 258/66/225 totals were released | Aider 2024-12-21 write-up |

### Sources checked directly (not just via search snippets)
- **Aider launch write-up** — methodology + the 697→225 filter, bucket totals only.
  https://aider.chat/2024/12/21/polyglot.html
- **`Aider-AI/polyglot-benchmark`** — exercise source code only; **no** results files,
  `.aider.results.json`, difficulty scores, or per-exercise data of any kind.
  https://github.com/Aider-AI/polyglot-benchmark
- **Aider LLM Leaderboards** — per-model aggregate only.
  https://aider.chat/docs/leaderboards/
- **Epoch AI — Aider Polyglot** — per-model pass-rate/edit-format/cost; a downloadable
  **CSV under Apache-2.0**, but one row per model, not per exercise.
  https://epoch.ai/benchmarks/aider-polyglot · terms: https://epoch.ai/benchmarks/use-this-data
- **llm-stats.com** — aggregate leaderboard (22 models; top GPT-5 0.880; field mean
  ≈0.581). https://llm-stats.com/benchmarks/aider-polyglot
- **Aider `benchmark/README.md`** — documents the `.aider.results.json` per-exercise
  output format and `pass_rate_#` aggregation (i.e. the raw material exists, but the
  harness aggregates it away into per-run stats).
  https://github.com/Aider-AI/aider/blob/main/benchmark/README.md

No GitHub gist, repo, spreadsheet, or paper surfaced that aggregates per-exercise
pass/fail across a range of models. (Some search snippets *claimed* "exercise-by-
exercise results on the tracking sites"; fetching those pages showed only per-model
aggregates — the claim did not hold up.)

---

## Implications & recommendation for danno's exercise ordering

Because the public record cannot give a per-exercise, cross-model difficulty order,
there are only two honest paths:

1. **Empirical, from our own runs (only rigorous option).** Run `danno bench` across
   the model field, then order exercises by observed mean `pass_rate_2` (or
   turns/tokens-to-solve). This yields difficulty **for our exact harness + model +
   sandbox triple** — the only ground truth that applies to us, and the one that
   matches the "low pass rate across current models" definition above. It is mildly
   circular for "which 10 to run first" (you run all, then re-rank), but it is the
   real signal and it accumulates across sweeps.

2. **Static proxy (cheap, reproducible, weak).** Rank by a per-exercise signal from the
   vendored source itself — reference-solution LOC, number of test cases, instruction
   length. Objective and needs no run, but a poor difficulty predictor given the
   pre-filtering, and not the metric we actually care about.

**Recommendation.** Do **not** ship the hand-authored "canonical Benchmark Exercise difficulty"
ordering as if it were difficulty-grounded — the research shows it cannot be. For the
committed bench3 configs, present the exercise list **unordered (alphabetical),
one-per-line**, with a header note that (a) no public per-exercise difficulty data
exists, (b) all 225 are already hard by construction, and (c) empirical mean
`pass_rate_2` from a danno sweep is the intended difficulty signal once available.
Optionally seed a first sweep, then re-order the list by our own measured pass rates
and record the run (git hash + model set) alongside it.

## Open follow-ups
- If we want a reusable artefact: after a sweep, emit a `difficulty.csv`
  (`exercise, models_run, mean_pass_rate_2, mean_turns`) under the bench output and
  order future `select` lists from it — the per-exercise dataset the public domain
  lacks, for our triple.
- Worth a periodic re-check: Aider or Epoch could publish per-exercise data later;
  as of 2026-07-17 neither does.
