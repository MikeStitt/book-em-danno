# UX proposal: the `danno validate` CLI

> **Status: PROPOSAL — not implemented.** This document proposes the command
> surface, interim status reporting, and outputs for a `danno validate` CLI that
> wraps the existing `danno_validator` harness. It is a UX design doc, a sibling to
> [`plan-danno-validator.md`](plan-danno-validator.md) (the harness plan) and
> [`../docs/ux-requirements.md`](../docs/ux-requirements.md) (danno's reconciled
> command surface). `.docs/` is exempt from markdown/format checks. Nothing here
> changes behaviour yet; it exists so the CLI is designed before it is built.

> **Implementation status (2026-06-18): SHIPPED**, except `--html`. The command
> (`danno validate`), live status reporting, `--dry-run` plan, the disposable
> sweep+baseline orchestration, `results.json`, `--strict`, and teardown are built
> and tested (`danno_validator/run.py`, `console.py`, `serialize.py`, `events.py`;
> the `cli.py` command). `--html` is **deferred to M7** — HTML rendering needs the
> Sphinx wiring that the (still-empty) `danno[validator]` extra will carry; until
> then the command rejects `--html` up front and writes the MyST report regardless.

## Why a CLI

The harness is complete and tested (M0–M5 + the M6 menu emitter), but it has **no
user-facing entry point**. Today a live run means hand-writing a Python driver
script in `scratch/` that imports the public API and orchestrates the flow:

```bash
uv run python scratch/m5_live_baseline.py      # the only way to run it live today
```

That script (gitignored, not held to repo standards) does, in order:
`prepare_workspace` → `provision` (opencode) → `run_sweep` → `provision` (claude) →
`run_baseline` → `write_sweep_report`. Every danno user who wants to know *which of
their declared models actually works* would have to write that script. A
`danno validate` command turns that orchestration into a first-class, discoverable
operation with progress reporting and stable outputs.

## Design principles (inherit danno's conventions)

1. **It runs immediately — `validate` is like `start`, not `install`.** Running the
   battery *is* the command's purpose, so `danno validate` provisions and sweeps
   directly, with no advise/`--apply` split. It joins `sandbox start`/`shell` as the
   interactive exception to danno's two-tier policy: those commands launch without
   `--apply` because launching is the point. `validate` is safe to run directly for
   the same reasons it's the exception — it never touches the user's repo (principle
   2) and it fails loud on prerequisites *before* doing expensive work (principle 4).
   A `--dry-run` flag prints the plan without running, for anyone who wants to
   preview an expensive sweep first — but it is opt-in, never the default.
2. **Never writes into the user's project.** The battery never runs in the user's
   project, and `validate` never edits the user's `danno.toml`. It creates
   **disposable, validator-owned** sandboxes over a **throwaway workspace** (seeded
   from a *copy* of the project's `danno.toml`), runs there, writes all output under
   its own run directory, and tears the sandboxes down. The user's real sandbox,
   agent-home, working tree, and `danno.toml` are untouched. The annotated menu is
   written to the run directory only; adopting it is a manual copy the user makes.
   This is the constitution's non-destructive rule (Working Rule 6) made visible.
3. **Reuse the shared flags.** `--target`/`-C`, `--agent`, `-v/--verbose` carry their
   existing danno meanings. Sandbox names derive the same way (`danno-<parent>-<dir>`)
   with a `validate` infix so they never collide with the user's real sandboxes.
4. **Fail loud (Working Rule 8), up front.** A missing `danno.toml`, an unreachable
   Ollama, an `--only` model the config doesn't declare, or (for `--baseline`) a
   missing `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` aborts *before* any sandbox
   is created — never a silent skip or a half-run sweep ten minutes in.
5. **The opencode-only invariant holds.** The agent-under-test runs *only* in the
   VM; the CLI, the oracles, and the report render on the host.

## Command surface

```
danno validate [--target DIR] [options]      # provision, sweep, write the report — one command
danno bench    [--target DIR] [options]      # run benchmark-task suites across the model matrix
```

### `danno bench` (sibling command — benchmark task suites)

Where `validate` answers *"does this model work"* (the L0→L2 liveness/tool/dev
battery), `danno bench` answers *"how does it do on real SWE benchmark tasks"*. It
runs the **suites** declared in `benchmarks.toml` — **Aider Polyglot** (Exercism
exercises) and a **SWE-bench Verified subset** (real GitHub issues, fetched from
HuggingFace) — for **every model variant** of `danno.toml` (the *permutations*),
against the chosen agent-under-test (`--agent claurst` for the Rust clone on local
models). It reuses the benchmark-task abstraction (`suites/`): each task seeds an
instance, the agent takes one headless turn, and the instance's own tests grade it
(the shared oracle classifies the turn). Aider shares one disposable sandbox
(per-exercise reset); SWE-bench uses a fresh sandbox per instance. Output is
`bench.json` + a console summary.

Key flags: `-C DIR` (project), `--agent {opencode,claurst}`, `--only MODEL` (subset
the matrix), `--benchmarks FILE` (default `benchmarks.toml` next to `danno.toml`),
`--workspace`, `--out`, `--keep-sandboxes`, `--dry-run`. See `benchmarks.toml.example`.

**Honesty note:** these run real benchmark task *content* via danno's own execution
model (a headless turn in a disposable sandbox), **not** the official Docker-per-task
harness — so the pass counts are *not* official benchmark scores. Local models rarely
*resolve* real SWE-bench instances; that tier exercises the harness, not a leaderboard.

---

A **single command with flags** — no subcommands. Running the tiered sweep and
writing the report is the whole job; the variations (which models, how many tiers,
with/without the baseline, where output goes) are flags, not verbs. HTML rendering
and the menu are produced inline by `--html`/`--menu`, so there is no separate
`render`/`menu` subcommand to learn. (Re-processing a prior run host-side without
re-sweeping is possible later off the persisted `results.json` — but as an optional
flag, not a new verb.)

### `danno validate` options

| Option | Default | Maps to | Meaning |
|---|---|---|---|
| `--target DIR`, `-C DIR` | `.` | `load_config(DIR/danno.toml)` | Project whose `danno.toml` is swept. Same flag as every other danno command. |
| `--dry-run` | off | — | Print the plan (models, tiers, sandboxes, output paths, cost caveat) and exit 0 without provisioning. Opt-in preview of an expensive run; **not** the default. |
| `--only MODEL` (repeatable) | all declared | `run_sweep(only=…)` | Restrict the sweep to these `danno.toml` model keys. Unknown key → fail loud. `--only` with one model is the fast "just check this one" path. |
| `--max-level {0,1,2}` | `2` | `level1=`, `level2=` toggles | Highest tier to attempt. `0` = liveness only (fast smoke test); `1` adds tool/bash; `2` adds software-dev. The short-circuit still applies *within* the cap. |
| `--baseline` | off | calls `run_baseline` | Also run the Claude Code baseline row for comparison. Requires a host token. |
| `--baseline-model ALIAS\|ID` | claude default | `run_baseline(model=…)` | Pin the baseline's claude model (`opus`/`sonnet`/`fable` or a full id). The *resolved* model is recorded on the row regardless. |
| `--judge` | off | `run_sweep(judge=…)` / `run_baseline(judge=…)` | Grade **L2 dev quality** (clarity, over-/under-build) with a host-side Anthropic judge, layered on top of the objective hidden-test verdict — never changes pass/fail. Needs `ANTHROPIC_API_KEY` (API billing, *not* the Claude Code subscription token) and the `danno[validator]` extra; fails loud up front if either is missing. The verdict lands in the L2 report section + `results.json`. |
| `--judge-model ALIAS\|ID` | `opus` | `make_judge(model=…)` | Pin the judge model (`opus`/`sonnet`/`haiku` or a full id). Recorded per-`Judgement` (pin-and-track, like the baseline). |
| `--trials N` | `1` | (new loop; see plan "Determinism") | Run each config N times and report a pass-rate instead of a single verdict. `N>1` is future work; `1` is today's behaviour. |
| `--workspace DIR` | `$TMPDIR/danno-validate-<slug>` | `prepare_workspace(workspace_root=…)` | The throwaway, validator-owned workspace mount. Must be safe to `git clean -fdx` (guarded by the ownership marker). |
| `--out DIR` | `.danno-validator/<timestamp>/` | `write_sweep_report(out_dir=…)` | Where the MyST report + pages + `results.json` are written. Gitignored by default. |
| `--menu [PATH]` | on → `<out>/menu.danno.toml` | `write_menu(out_path=…)` | Emit the annotated menu danno.toml into the run dir. `--no-menu` to skip. Never written into the user's project (principle 2). |
| `--html` | off | sphinx-build | After writing MyST, render it to HTML in `<out>/html/`. |
| `--keep-sandboxes` | off (tear down) | skip `stop`/`rm` | Leave the disposable sandboxes running for debugging instead of removing them. |
| `--no-reset` | reset on | `run_sweep(reset=False)` | Skip the guarded per-config workspace reset (debugging only; configs can then leak into each other). |
| `--strict` | off | exit code | Exit non-zero if any *swept* config fails its top tier (the baseline never affects the exit code). For CI gating. Default exit 0 — it's a reporting tool. |
| `--agent NAME` | `opencode` | `provision(agent=…)`, `run_sweep(agent=…)` | The AUT for the *sweep* (the local models). The baseline is always `claude`. |
| `--env KEY=VAL` (repeatable) | — | `run_sweep(env_file=…)` | Inject a credential into the sweep's opencode exec for cloud configs (e.g. `--env ANTHROPIC_API_KEY=sk-…`). Overrides the host-exported value. Lands only in a chmod-600 env-file, removed after the sweep. |
| `--env-file FILE` (repeatable) | — | `run_sweep(env_file=…)` | Same, from a file of `KEY=VAL` lines. |
| `--verbose`, `-v` | off | `Runner(verbose=True)` | Stream the agent transcripts live under each tier line. |

**Cloud-config credentials.** A swept `cloud`/`openai`-backed model (anthropic,
NVIDIA NIM, …) needs an API key inside the disposable sandbox or it errors at L0
("x-api-key header is required"). danno auto-injects host-exported keys it can
identify — every `{env:VAR}` the generated `opencode.jsonc` references (e.g.
`NVIDIA_API_KEY`) plus each cloud provider's `<PROVIDER>_API_KEY` (anthropic →
`ANTHROPIC_API_KEY`, which opencode's built-in provider resolves implicitly, so it
is *not* a `{env:}` ref). Use `--env`/`--env-file` to supply or override. Unlike a
missing `--baseline` token (a hard abort), a missing *sweep* key only **warns** —
that one config errors loudly in its own row while the rest of the sweep proceeds,
since a run may legitimately target only the local models. Local Ollama models
need no credentials (their base URL is baked into the config).

Tiers and tasks beyond the curated defaults (custom `ScriptedTurn`/`Level1Task`/
`Level2Task`, larger task banks, the `--full` mode) stay in the Python API for now;
the CLI exposes the curated default battery. That keeps the first CLI small.

## Preview output (`--dry-run`)

`danno validate` runs immediately. For an expensive sweep, `--dry-run` resolves
everything and prints the plan — no sandbox, no model run — then exits:

```text
$ danno validate --target ./my-project --baseline --baseline-model opus --only gemma3-27b --only gpt-oss-20b --dry-run

danno validate — plan (--dry-run; drop --dry-run to execute)

  config        ./my-project/danno.toml
  declared      gemma3-27b, gpt-oss-20b, sonnet, nemotron        (4 models)
  sweeping      gemma3-27b, gpt-oss-20b                          (2 — via --only)
  tiers         L0 liveness · L1 tool/bash · L2 software-dev     (--max-level 2)
  baseline      Claude Code @ opus                               (--baseline)
  workspace     /tmp/danno-validate-my-project   (throwaway, validator-owned)
  report        .danno-validator/2026-06-18T14-30/
  menu          .danno-validator/2026-06-18T14-30/menu.danno.toml

  would create, then tear down, 2 disposable sandboxes:
    danno-validate-projects-my-project           opencode  (local-model sweep)
    danno-validate-projects-my-project-claude    claude    (baseline)

  ⚠ local models run sequentially and are slow (minutes per tier); the Claude
    baseline makes paid API calls. Your project is NOT modified — the battery runs
    in the throwaway workspace above, seeded from your danno.toml.

Drop --dry-run to provision, sweep, and write the report.
```

If `--baseline` is set but no token is exported, the run **fails loud here** —
before any sandbox is created, not after a 10-minute sweep (and `--dry-run`
surfaces it too):

```text
  ✗ --baseline needs a Claude token, but neither CLAUDE_CODE_OAUTH_TOKEN nor
    ANTHROPIC_API_KEY is set. Fix:
        claude setup-token            # Max/Pro, no per-token billing
        export CLAUDE_CODE_OAUTH_TOKEN=...
    or drop --baseline to sweep local models only.
```

## Interim status reporting (during a run)

A sweep can run for many minutes, so the CLI must show **what it is doing right
now** and **what each config scored as it finishes** — never a silent hang. The
model is: one `▶` step header per phase, a live spinner+elapsed line for the
in-flight tier, and a one-line verdict roll-up per config.

```text
$ danno validate --target ./my-project --baseline --baseline-model opus

▶ prepare workspace  /tmp/danno-validate-my-project
    seeded marker · generated .opencode/opencode.jsonc · git commit ✓

▶ provision opencode sandbox  danno-validate-projects-my-project
    create ✓ · egress policy (allow-host localhost:11434) ✓ · stopped ✓

▶ sweep 1/2  gemma3-27b   ollama/gemma3:27b
    L0 liveness …  ⠹ 0:38
    L0  ! error — model does not support tools (ollama API 400)
    → L1, L2 skipped (L0 did not pass)
  ┄ gemma3-27b    L0 ! error     L1 –       L2 –                  0 tok    0:38

▶ sweep 2/2  gpt-oss-20b   ollama/gpt-oss:20b
    L0 liveness …  ✓ pass                                      19.1k tok  1:39
    L1 tool/bash …  ✓ pass  (line_count.txt == "7")               …       0:42
    L2 software-dev …  ⠴ 1:12
    L2  ✗ early-stop — hidden tests exit 1 (printed the answer, made no edit)
  ┄ gpt-oss-20b   L0 ✓ pass    L1 ✓ pass   L2 ✗ early-stop      47.0k tok  3:41

▶ provision claude sandbox  danno-validate-projects-my-project-claude
    create ✓ · auth env-file (chmod 600) ✓

▶ baseline  Claude Code @ opus
    L0 …  ✓ pass     L1 …  ✓ pass     L2 …  ✓ pass
  ┄ claude-code   L0 ✓ pass    L1 ✓ pass   L2 ✓ pass            2.0k tok  0:21
    resolved model: claude-opus-4-8

▶ tear down sandboxes  danno-validate-projects-my-project(+-claude) … ✓
```

Reporting rules that fall out of the harness's real behaviour:

- **Short-circuit is shown, not hidden.** When a tier fails, the skipped higher
  tiers print `→ L1, L2 skipped (L0 did not pass)` and render as `–` (not `✗`), so
  "skipped" never reads as "failed". This mirrors the report's `—` cells.
- **The verdict mark set matches the report:** `✓ pass`, `✗ <class>` (the
  `FailureClass`, e.g. `early-stop`, `stall`), `~ only-acts-on-nudge`, `! error`,
  `– not run`.
- **Per-tier latency + tokens stream as each tier finishes** (the harness already
  records `latency_s`/`tokens` per turn), so a slow local model shows progress
  rather than a frozen prompt.
- **The baseline row prints its resolved model** (read from the claude `system`
  init event), exactly as the report records it — `opus` may resolve to
  `claude-opus-4-8` (or `…[1m]` unpinned), and the row shows the truth.
- **`-v/--verbose`** expands each tier to stream the prompt sent and the assistant
  reply beneath the spinner line, for debugging a surprising verdict.

## Final summary output

After teardown, the CLI prints the same data the report's index matrix carries, so
the terminal is useful on its own:

```text
── results ──────────────────────────────────────────────────────────────────
  config         L0        L1        L2                tokens   latency
  gemma3-27b     ! error   –         –                      0      0:38
  gpt-oss-20b    ✓ pass    ✓ pass    ✗ early-stop       47.0k      3:41
  claude-code    ✓ pass    ✓ pass    ✓ pass   (base)     2.0k      0:21

  swept: 2 configs · 1 cleared L0 · 0 cleared all tiers
  taxonomy: error 1 · early-stop 1
  baseline: claude-opus-4-8 cleared all tiers (reference)

  report   .danno-validator/2026-06-18T14-30/index.md      (3 pages)
  menu     .danno-validator/2026-06-18T14-30/menu.danno.toml
           ↳ uncomment the [agents] assignment you want, copy the block back into
             your danno.toml yourself (validate never edits it)
  results  .danno-validator/2026-06-18T14-30/results.json   (machine-readable)

  re-run with --html to also render an HTML report.
```

The baseline is flagged `(base)` and **excluded from the swept tally and the
taxonomy** (it describes the models under test) — identical to the report's index.

## Outputs (where everything goes)

All run output lands under one timestamped directory (default
`.danno-validator/<ISO-timestamp>/`, already gitignored):

| File | Produced by | Purpose |
|---|---|---|
| `index.md` | `write_sweep_report` | MyST results matrix + failure taxonomy + toctree |
| `level0-<model-slug>.md` (one per config) | `write_sweep_report` | per-config page: L0 transcript, L1/L2 sections, signals, tokens, latency |
| `menu.danno.toml` | `write_menu` | the annotated "menu" config: every `[models.*]` block tagged with its `[L0 · L1 · L2]` verdict, `[agents]` as a comment/uncomment menu |
| `results.json` | the CLI | machine-readable run record — the contract for CI (`--strict`), external dashboards, and future host-side re-rendering without re-sweeping. Defined below. |
| `html/` *(with `--html`)* | sphinx-build | rendered HTML report |

Console output (the live status + final summary) is the third "output" — designed
so a user who never opens the report still gets the answer.

**Exit codes:** `0` always, except `--strict` returns `1` when any swept config
fails its top tier (baseline never affects the code). Hard errors (no `danno.toml`,
unreachable Ollama, missing token, Docker `sandbox` absent) exit non-zero with a
fix — in both a normal run and `--dry-run`.

### `results.json` (the run record)

The one structured artifact, so everything downstream — CI gates, a future
dashboard, host-side re-rendering — reads data instead of scraping the MyST. It is a
direct, lossless serialization of the `list[SweepResult]` the harness already
returns (`SweepResult` → `ConversationResult` / `TaskResult` / `DevTaskResult` →
`TurnVerdict` / `TestRun`), plus run metadata. `schema_version` is bumped on any
breaking change.

```jsonc
{
  "schema_version": 1,
  "tool": "danno-validate",
  "danno_version": "0.3.0",
  "generated_at": "2026-06-18T14:30:05Z",      // host clock, ISO-8601 UTC

  "config": {
    "path": "/Users/me/projects/my-project/danno.toml",
    "declared_models": ["gemma3-27b", "gpt-oss-20b", "sonnet", "nemotron"]
  },

  "run": {                                       // exactly what was asked for
    "swept_models": ["gemma3-27b", "gpt-oss-20b"],
    "max_level": 2,
    "trials": 1,
    "reset": true,
    "agent": "opencode",
    "workspace": "/tmp/danno-validate-my-project",
    "out_dir": ".danno-validator/2026-06-18T14-30",
    "sandboxes": { "sweep": "danno-validate-projects-my-project",
                   "baseline": "danno-validate-projects-my-project-claude" },
    "baseline": { "enabled": true, "requested_model": "opus" }
  },

  "results": [                                   // one entry per matrix row, in order
    {
      "model_name": "gpt-oss-20b",              // danno.toml key (variant.model_name)
      "model_ref": "ollama/gpt-oss:20b",        // resolved -m ref (variant.model_ref)
      "is_baseline": false,
      "recommended": false,                     // menu.is_recommended (all tiers pass)
      "badge": "[L0 ✓ · L1 ✓ · L2 ✗ early-stop]",   // menu.verdict_badge

      "level0": {                               // ConversationResult
        "overall": "pass",                      // FailureClass value
        "passed": true,
        "session_id": "ses_01H…",
        "tokens": 19104,
        "cost": 0.0,                            // 0 for local; USD for cloud
        "latency_s": 99.1,
        "turns": [                              // one per scripted turn (TurnRecord)
          {
            "label": "greet",
            "prompt": "Hello! In one short sentence…",
            "assistant_text": "I help you write and debug code.",
            "tool_calls": [],                   // [{ "tool": "bash", "status": "completed" }, …]
            "tokens": 1200,
            "latency_s": 12.0,
            "errors": [],
            "verdict": {                        // TurnVerdict
              "failure_class": "pass",
              "promised_action": false,
              "tool_call_count": 0,
              "side_effect": false,
              "rationale": "coherent reply to a conversational turn."
            }
          }
        ]
      },

      "level1": {                               // TaskResult, or null if not run
        "task_label": "line-count",
        "overall": "pass",
        "passed": true,
        "session_id": "ses_01H…",
        "tokens": 5012,
        "latency_s": 42.0,
        "assistant_text": "There are 7 lines; wrote 7 to line_count.txt.",
        "tool_calls": [{ "tool": "bash", "status": "completed" }],
        "errors": [],
        "verdict": { "failure_class": "pass", "promised_action": false,
                     "tool_call_count": 1, "side_effect": true,
                     "rationale": "made 1 tool call(s); the workspace changed as required." }
      },

      "level2": {                               // DevTaskResult, or null if not run
        "task_label": "fizzbuzz",
        "overall": "early-stop",
        "passed": false,
        "session_id": "ses_01H…",
        "tokens": 22980,
        "latency_s": 110.4,
        "assistant_text": "Here is the FizzBuzz implementation: …",
        "tool_calls": [{ "tool": "bash", "status": "completed" },
                       { "tool": "read", "status": "completed" }],
        "errors": [],
        "verdict": { "failure_class": "early-stop", "promised_action": false,
                     "tool_call_count": 2, "side_effect": false,
                     "rationale": "tool call(s) completed but no workspace side effect was observed." },
        "test_run": {                           // TestRun — the hidden suite, run in-VM
          "command": "python3 hidden_test_fizzbuzz.py",
          "returncode": 1,
          "passed": false,
          "stdout": "AssertionError: fizzbuzz(3) == 'Fizz'",
          "stderr": ""
        }
      }
    },

    {
      "model_name": "gemma3-27b",
      "model_ref": "ollama/gemma3:27b",
      "is_baseline": false,
      "recommended": false,
      "badge": "[L0 ! error · L1 – · L2 –]",
      "level0": { "overall": "error", "passed": false, "session_id": null,
                  "tokens": 0, "cost": 0.0, "latency_s": 0.4,
                  "turns": [ /* … with verdict.failure_class = "error" … */ ] },
      "level1": null,                           // short-circuited (L0 didn't pass)
      "level2": null
    },

    {
      "model_name": "claude-code",              // BASELINE_MODEL — the reference row
      "model_ref": "claude-opus-4-8",           // the resolved model claude reported
      "is_baseline": true,
      "requested_model": "opus",                // what --baseline-model asked for
      "resolved_model": "claude-opus-4-8",      // what claude actually ran (from system init)
      "recommended": true,
      "badge": "[L0 ✓ · L1 ✓ · L2 ✓]",
      "level0": { "…": "…" }, "level1": { "…": "…" }, "level2": { "…": "…" }
    }
  ],

  "summary": {                                  // swept configs only — baseline reported apart
    "swept_total": 2,
    "passed_l0": 1,
    "passed_all_tiers": 0,
    "taxonomy": { "error": 1, "early-stop": 1 },   // Counter over swept configs' L0 class
    "baseline": { "model": "claude-opus-4-8", "passed_all_tiers": true }
  }
}
```

Schema notes:

- **`overall` / `failure_class`** are `FailureClass` string values: `pass`, `stall`,
  `only-acts-on-nudge`, `hallucinated-tool`, `refusal`, `early-stop`,
  `malformed-tool-args`, `loop`, `error`. The `summary.taxonomy` is the same
  `Counter` the report renders, over **swept configs only** (the baseline is the
  reference, excluded — as in the index page).
- **`level1` / `level2` are `null` when short-circuited** (an earlier tier didn't
  pass) — never an empty object, so "skipped" is unambiguous and matches the report's
  `—`. A reader checks `level2 != null && level2.passed` for "cleared L2".
- **`tool_calls`** is normalized to `[{ "tool", "status" }]` across agents (opencode
  `tool`/`tool_use` deduped by callID; claude `tool_use` + `tool_result.is_error`), so
  a consumer never branches on the agent. `cost` is `0.0` for local Ollama models and
  populated (USD) for cloud/baseline.
- **Baseline row** is the only one carrying `requested_model` + `resolved_model`; it
  always has `is_baseline: true` and `model_name == "claude-code"` (the
  `baseline.BASELINE_MODEL` sentinel the reporter keys off).
- **`trials > 1` (future)** extends each `levelN` with a `runs: [...]` array of the
  per-trial records plus an aggregate `pass_rate`; `schema_version` bumps when it
  lands. For `trials: 1` (today) the single-record shape above is canonical.
- **Stability contract.** Field renames/removals or a semantic change bump
  `schema_version`; additive optional fields do not. CI and any dashboard pin the
  major they understand.

## Prerequisites & `doctor` integration

A live `validate` needs everything `danno install` needs (Docker `sandbox`, Ollama
on `0.0.0.0:11434`) plus, for `--baseline`, a Claude token. Rather than duplicate
those checks, `validate` runs the relevant subset of `danno doctor` up front (and so
does `--dry-run`) and prints any FAIL/WARN with its existing copy-paste fix.
`validate` should not start a multi-minute sweep on a host that `doctor` would have
flagged in a second.

## Mapping to the existing API (implementability)

The CLI is a thin orchestrator over functions that already exist and are tested —
no new harness logic, just argument parsing, the progress layer, and `results.json`:

| CLI concern | Existing call |
|---|---|
| load + validate config | `book_em_danno.config.loader.load_config` |
| seed throwaway workspace | `sweep.prepare_workspace(runner, ws, config)` |
| create/tear down sandboxes | `commands.sandbox.provision` / `.stop` (+ `docker sandbox rm`) |
| local-model sweep | `sweep.run_sweep(runner, name, config=…, workspace_root=…, only=…, level1=…, level2=…, reset=…, agent=…)` |
| Claude baseline row | `baseline.run_baseline(runner, name, workspace_root=…, model=…, level1=…, level2=…)` |
| write report | `report.write_sweep_report(results, out_dir)` |
| write menu | `menu.write_menu(config, results, out_path, verified=…)` |
| execution / live transcripts | `core.exec.Runner(apply=True, verbose=…)` (validate always executes) |
| `results.json` | new — serialize the returned `list[SweepResult]` + run metadata |

The progress reporting is the one genuinely new piece: today `run_sweep` runs the
whole loop and returns; surfacing per-config/per-tier status either means a
callback/event hook on `run_sweep`/`run_tiers`, or the CLI iterating
`matrix.model_variants` itself and calling `run_tiers` per model so it owns the
loop (and thus the progress emission). The latter keeps the harness unchanged and
is the lighter first cut.

## Decisions (resolved 2026-06-18)

1. **Runs immediately, like `start` — no advise/`--apply` split.** `validate` joins
   `sandbox start`/`shell` as the interactive exception to the two-tier policy: its
   purpose is to run. `--dry-run` is an opt-in preview, never the default gate.
2. **One `danno validate` with flags — no subcommands.** Variations are flags;
   HTML/menu are produced inline by `--html`/`--menu`. No `run`/`render`/`menu` verbs.
3. **Never writes into the user's project.** The menu is only ever written into the
   run directory; the user copies blocks into `danno.toml` by hand. There is no
   `--write-menu-to ./danno.toml` — applying the menu stays a deliberate manual step.
4. **`results.json` is defined now** (see [the schema above](#resultsjson-the-run-record)),
   as the structured contract for CI, dashboards, and future host-side re-rendering.

### Still open (smaller, for implementation time)

- **Progress hook vs CLI-owned loop.** Whether to add a callback to
  `run_sweep`/`run_tiers` for per-tier progress, or have the CLI own the
  `model_variants` loop and call `run_tiers` per model (lighter; harness unchanged).
- **`--trials N` aggregation.** The pass-rate shape and the `results.json` `runs[]`
  extension — design when `N>1` is actually built.
- **`--dry-run` cost estimate.** Whether to show a rough token/time estimate or only
  the structural plan (no reliable basis for local-model timing yet).
