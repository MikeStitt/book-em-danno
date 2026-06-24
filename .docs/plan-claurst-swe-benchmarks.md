# Plan — Claurst as a first-class agent-under-test + SWE benchmark tiers

> **Status:** design of record. Written 2026-06-23. Not yet implemented.
> **Branch base:** stack on `remove-tool-call` (or branch from `main` if that has merged).
> **Companions:** [`.docs/claude-code-clones-research.md`](claude-code-clones-research.md) (why Claurst),
> [`.docs/plan-danno-validator.md`](plan-danno-validator.md) (the harness this extends),
> [`.docs/ux-danno-validate-cli.md`](ux-danno-validate-cli.md) (CLI surface).
> **Memory:** [[sandbox-egress-and-process-lifetime]] (the relay + child-reaping constraints).

## Context — why

danno's research (`claude-code-clones-research.md` §3–4) names **Claurst** (`Kuberwastaken/claurst`,
pure-Rust, `claurst -p` headless, native Ollama) as the best local-first Claude-Code clone to wire
into danno, and recommends benchmarking it by adding it as a **new agent-under-test in danno's own
L0→L1→L2 harness** rather than dragging in the heavyweight industry suites.

Two things sharpen that recommendation:

1. **"a row in the harness" only solves the *agent* axis.** It teaches the harness *how to invoke and
   read* Claurst. It does **not** raise benchmark quality — danno's current L2 is a single FizzBuzz
   graded by one hidden test. High-quality SWE benchmarking is a separate *task* axis.
2. **The egress/pip "blocker" was disproven by spike (2026-06-23).** In the proxy-only sandbox:
   wheels (`requests`, `numpy`), `git clone`, `apt-get install build-essential`, and `curl` to
   pypi (5/5 @ <130 ms) all work. pip has an *intermittent pooled-HTTPS ReadTimeout* through the
   proxy — but `curl` to the same host is rock-solid, proving it's a pip-config quirk, not an egress
   wall. Mitigations proven: `pip install --no-cache-dir`, and curl-fetch-wheel → `pip install
   --no-index --find-links` (offline). So a **SWE-bench Verified subset is viable in-sandbox**; its
   only real cost is the per-instance environment provisioning every SWE-bench runner faces.

**Outcome:** Claurst becomes a selectable agent-under-test, and the validator gains **two benchmark
suites** — **Aider Polyglot** (self-contained exercises) and a **SWE-bench Verified subset** (real
GitHub issues) — each **enabled/selected by config**, with **per-test sandbox isolation** for SWE-bench.
We run *real benchmark task content via danno's execution model*; we never claim an official
Docker-per-task "SWE-bench Verified score."

## Architecture — two orthogonal axes

```
            AGENT axis (Part A)                 TASK axis (Part B)
        how we invoke + read an AUT          what problems + how graded
   ┌──────────────────────────────────┐  ┌────────────────────────────────────┐
   │ opencode_run  (exists)           │  │ L0 liveness / L1 tool / L2 dev (exist)│
   │ claude_run    (exists, baseline) │  │ AiderTask        (new)                │
   │ claurst_run   (NEW) ── TurnFn    │×│ SwebenchTask     (new)                │
   └──────────────────────────────────┘  └────────────────────────────────────┘
                    │                                   │
                    └────── reused, agent-agnostic ─────┘
                         oracle.classify_turn(side_effect=tests_passed)
```

The existing `Turn`/`TurnFn` protocol in `driver.py` is the agent seam; the existing `Level2Task`
`seed → run → grade` contract is the task seam. Both benchmark suites map onto that contract. The
oracle, `run_tiers`, and the level runners stay agent- and task-agnostic.

---

## Part A — Claurst as an agent-under-test (the driver row)

### A.1 Hard constraints (from [[sandbox-egress-and-process-lifetime]])
- **Claurst's Rust HTTP client ignores `HTTPS_PROXY`**, and the sandbox blocks direct egress +
  rejects CONNECT tunnels. So Claurst cannot reach host Ollama (or cloud) the normal way. It must be
  pointed at **`127.0.0.1:11434`** (in `NO_PROXY`), where a **VM-local relay** forwards to host
  Ollama *through the squid proxy* via regular proxied HTTP.
- **Execs reap their children.** The relay cannot be a persistent background process — it must be
  launched **inside the same `exec`** as each `claurst -p` call and killed when the call returns
  (`python3 relay & … claurst -p … ; kill %1`). `python3` is present in the shell VM.
- **Claurst is not in the prebuilt sandbox roster** (claude/opencode/codex/copilot/gemini/cagent/
  kiro/shell) → it must be **installed into a `shell` (or `opencode`) sandbox** during provisioning.

### A.2 New code
- **`ClaurstTurn`** + **`claurst_run()`** in `src/danno_validator/driver.py`, matching the existing
  `Turn` / `TurnFn` protocols (mirror `ClaudeTurn` / `claude_run`). Parses Claurst's headless output
  (format pinned by spike S2) into `assistant_text` / `tool_calls` / `tool_call_count` / `session_id`
  / `tokens` / `cost` / `errors`.
- **Relay wrapper** (`_claurst_exec` helper): builds the single `docker sandbox exec` command that
  (1) starts the python relay on `127.0.0.1:11434`, (2) waits for readiness, (3) runs `claurst -p`
  pointed at the relay, (4) kills the relay. Lives next to `claurst_run`.
- **Install step** in `src/book_em_danno/commands/sandbox.py`: install Claurst into the sandbox via
  the S1-proven method (preferred: curl-fetch the prebuilt aarch64 binary — `curl` is proven through
  the proxy; fallbacks: `npm i -g claurst`, `install.sh`). Hooked post-provision like `run_npm_setup`.
- **AUT selection**: generalize agent-under-test choice. Today `--agent` selects an *opencode*
  subagent and `claude` is a hardcoded baseline path (`baseline.py`). Add an explicit AUT dispatch
  `{opencode, claude, claurst}` → `{opencode_run, claude_run, claurst_run}`, mirroring `baseline.py`
  with a new `claurst.py` run path (auth/env + relay setup, then `run_tiers(run_turn=claurst_run)`).

### A.3 M0 spike findings (verified 2026-06-23, `danno-claurst-spike`)
- **S1 install:** `npm i -g claurst` **fails** — its `install.js` downloads the binary direct from
  GitHub (`ECONNREFUSED 140.82.113.4:443`), bypassing the proxy. **Use curl:** fetch
  `…/releases/download/v0.1.5/claurst-linux-aarch64.tar.gz` through the proxy (1.7 s), extract the
  `claurst` binary to `~/.local/bin` (already first on PATH).
- **S2 output format — Claurst's own, NOT Claude's.** `--output-format stream-json` emits JSONL:
  - `{"text":"…","type":"text_delta"}` — assistant text (concatenate deltas).
  - `{"tool":"Write","type":"tool_start"}` — tool call start (name only; **no callID/status**).
  - `{"cost_usd":0.0,"type":"result","usage":{"input_tokens":0,"output_tokens":0}}` — terminal.
  Model ref is `-m ollama/<tag>`. `--dangerously-skip-permissions`/`--yolo` skips prompts.
  **Gotchas:** (1) **no `session_id`** is emitted and `~/.claurst` is never created in headless Ollama
  mode → **multi-turn session continuation is unavailable**; `ClaurstTurn.session_id` is `None` and
  each scripted turn runs independently (fine for L1/L2/benchmarks; L0's nudge becomes a standalone
  re-prompt — graded by side effect anyway). (2) **tokens/cost are 0** for Ollama (informational
  only). (3) **`--verbose` dumps ANSI DEBUG logs to stdout** → never use it for parsing.
- **S3 relay:** a VM-local python forwarder on `127.0.0.1:11434` re-issuing to
  `host.docker.internal:11434` through the proxy works; Claurst reaches it via
  `OLLAMA_HOST=http://127.0.0.1:11434`, co-located in the same child-reaping exec. Prototype:
  `scratch/claurst-spike-ws/ollama_relay.py`.
- **Turn-protocol mapping:** `assistant_text` = Σ `text_delta.text`; `tool_calls` =
  `tool_start` events → `[{tool: name, callID: None, state: {status: "completed"}}]`;
  `tool_call_count` = count; `session_id` = None; `tokens`/`cost` from `result` (0 for Ollama);
  `errors` = [] (transport errors surface as nonzero exit / a `result.result` error string).
  Grade Claurst by **side effects** (L1/L2/benchmarks already do); relax tool-call-dependent L0
  verdicts since there is no per-call status.

---

## Part B — Benchmark task tiers (both suites)

### B.1 Task abstraction (`src/danno_validator/benchmarks/`)
A new package, generalizing `Level2Task`:

```python
class BenchTask(Protocol):
    id: str
    prompt: str
    def provision(self, runner, sandbox, ws) -> None: ...   # clone repo / seed stubs / install deps
    def reset(self, ws) -> None: ...                        # restore to base between agent runs
    def grade(self, runner, sandbox, ws) -> BenchVerdict: ...# run the task's test set → pass/fail
```

Grading reuses `oracle.classify_turn(turn, side_effect=tests_passed, expects_action=True)` so a
benchmark row produces the same verdict taxonomy as the tiers.

### B.2 Aider Polyglot (`benchmarks/aider.py`) — lightweight, shared sandbox
- **Source:** `git clone` the polyglot exercise set (proven through proxy). Each exercise = stub
  files + a test file across Python/Rust/Go/JS/C++/Java.
- **`AiderTask`:** `provision` = copy the exercise's stub into the workspace; `prompt` = the
  exercise instructions; `grade` = run the exercise's test command (`pytest` / `cargo test` /
  `go test` / …). Multi-language exercises Claurst beyond Python.
- **Isolation:** default **shared sandbox + workspace reset per exercise** (exercises are
  self-contained, no heavy deps). Per-exercise isolation available via config.
- **Toolchain:** language runtimes installed once into the shared sandbox during provisioning
  (apt through proxy, proven). Rust/Go added only if those languages are selected.

### B.3 SWE-bench Verified subset (`benchmarks/swebench.py`) — per-test sandbox
- **Dataset:** the `SWE-bench_Verified` instance metadata (repo, `base_commit`, `FAIL_TO_PASS`,
  `PASS_TO_PASS`, test command). Fetched once host-side (or via proxy) into a pinned manifest.
- **`SwebenchTask`:** `provision` = `git clone <repo>` @ `base_commit` into the test's workspace,
  then install the instance's deps using the **offline wheel-cache strategy** (curl/`pip download`
  the wheels → `pip install --no-index --find-links`, the spike-proven mitigation; `apt` for system
  libs; `--no-cache-dir` fallback). `prompt` = the GitHub issue text. `reset` = `git reset --hard
  base_commit && git clean -fd`. `grade` = run `FAIL_TO_PASS` (must pass) + `PASS_TO_PASS` (must stay
  passing).
- **Isolation: one disposable sandbox per instance** (the user's call, and the right one — each
  instance has its own repo + dep tree; baking them into one box risks cross-contaminated grading
  and matches SWE-bench's per-instance-image model). Deps installed once per instance sandbox;
  source reset between agent/model variants.
- **Starter subset:** 3–5 curated instances known to install cleanly offline (pure-Python or
  wheel-only deps), so the path is proven before scaling. **Fail loud** ([Working Rule 8]) and
  `log()` exactly which instances were skipped/dropped — never silently shrink coverage.

### B.4 Per-test sandbox lifecycle (new orchestration loop in `run.py`)
Distinct from `run_sweep` (one sandbox, many model variants):

```
for test in enabled_swebench_tests:
    box = provision_fresh_sandbox(agent, ws_for(test))      # derive name: <base>-bench-<test.id>
    test.provision(runner, box, ws_for(test))               # clone@base + offline deps
    for variant in agent_matrix:                            # claurst×models, claude, opencode×models
        test.reset(ws_for(test))                            # git reset --hard
        turn = run_turn[variant](runner, box, test.prompt, model=variant.model_ref, workspace=ws)
        verdict = test.grade(runner, box, ws_for(test))
        record(test, variant, verdict)
    teardown(box)                                           # docker sandbox rm (unless --keep-sandboxes)
```

Aider Polyglot uses the shared-sandbox variant of the same loop (provision once, reset per exercise).

---

## Part C — Configuration (enable + select, both suites)

A new `[benchmarks]` table (in `danno.toml`, or a sibling `benchmarks.toml` referenced by
`--benchmarks <file>`), with pydantic models in `config/schema.py` (or validator-local):

```toml
[benchmarks.aider_polyglot]
enabled   = true
isolation = "shared"          # shared (default) | per-test
source    = "git:https://github.com/Aider-AI/polyglot-benchmark"
select    = ["python/anagram", "rust/clock", "go/grep"]   # explicit ids | "all" | by-language tag

[benchmarks.swebench]
enabled    = true
isolation  = "per-test"       # per-test (default for swebench) | shared
dataset    = "princeton-nlp/SWE-bench_Verified"
deps       = "offline-wheel-cache"   # offline-wheel-cache (default) | no-cache-dir
select     = ["django__django-11099", "sympy__sympy-20154"]   # curated subset ids
```

- `enabled` gates each suite; `select` identifies which tests run (the user's "which tests we
  enable"). `extra="forbid"` so a typo fails loud at load.
- CLI: `danno validate --agent claurst --benchmark {aider,swebench,all}` plus the existing `--only`
  model filter to bound the matrix. AUT × suite × selected-tests × models is large — config selection
  is the throttle.

---

## Milestones (each ends with `ninja check` green)

**M0 — De-risk spikes** (constitution "Config is code: verify by exercising"; no production code):
- **S1** Claurst install in the sandbox — try `npm i -g claurst`; if the postinstall binary fetch
  bypasses the proxy and fails, fall back to **curl-fetching the prebuilt aarch64 binary** (curl
  proven) or `install.sh`. Record the working method.
- **S2** Claurst headless output — `claurst -p --help`; run a greet + a file-write task; capture the
  stdout shape (JSON/stream-json/text) and the session-continuation flag for multi-turn L0.
- **S3** Claurst → Ollama via loopback relay — stand up the python relay on `127.0.0.1:11434` →
  host Ollama through the proxy, co-located in one exec; point Claurst at it; confirm a real
  completion and that child-reaping forces same-exec co-location.
- **S4** Benchmark data — confirm Aider Polyglot `git clone` and one SWE-bench instance's
  repo@base + offline-wheel-cache dep install + `FAIL_TO_PASS` test run end-to-end (extends the
  2026-06-23 pip spike).

**M1 — Claurst driver row:** `ClaurstTurn` + `claurst_run` + relay wrapper + parser; unit tests
mocking `subprocess` (mirror `tests/test_validator_driver_claude.py`).

**M2 — Claurst provision/install:** install step in `sandbox.py`; env/auth wiring; `claurst.py` run
path (mirror `baseline.py`).

**M3 — AUT selection:** generalize `{opencode, claude, claurst}` dispatch through `run_tiers` /
`run.py`; `--agent claurst` end-to-end against the existing L0→L1→L2 tiers (host Ollama).

**M4 — Benchmark abstraction + config:** `benchmarks/` package (`base.py`, `config.py`),
`BenchTask`/`BenchVerdict`, `[benchmarks]` schema + loader; unit tests for seed/reset/grade with
fake turns.

**M5 — Aider Polyglot suite:** `aider.py`; shared-sandbox loop; report rows; run a small `select`
against the **claude baseline first** (reliable cloud), then claurst+Ollama.

**M6 — SWE-bench Verified subset:** `swebench.py`; **per-instance sandbox lifecycle**; offline
wheel-cache dep install; 3–5 curated instances; report rows.

**M7 — Reporting + docs:** extend `results.json` + sweep report with benchmark rows; update
`--help`, `ux-danno-validate-cli.md`, `danno.toml.example`, CHANGELOG; document explicitly that this
is *real benchmark tasks via danno's execution model*, not the official Docker-per-task harness.

## Critical files
- `src/danno_validator/driver.py` — `ClaurstTurn`, `claurst_run`, relay helper.
- `src/danno_validator/claurst.py` (NEW) — claurst run path (mirror `baseline.py`).
- `src/danno_validator/sweep.py`, `run.py` — AUT dispatch; per-test sandbox loop; teardown.
- `src/danno_validator/benchmarks/{__init__,base,config,aider,swebench}.py` (NEW).
- `src/book_em_danno/commands/sandbox.py` — install-claurst exec; relay launch.
- `src/book_em_danno/config/schema.py` — `[benchmarks]` models.
- `src/danno_validator/cli.py` — `--agent claurst`, `--benchmark {aider,swebench,all}`.
- `tests/test_validator_driver_claurst.py`, `tests/test_benchmarks_*.py` (NEW, mirror existing).
- `danno.toml.example`, `.docs/ux-danno-validate-cli.md`, `CHANGELOG.md`.

## Verification (end-to-end)
- **Unit:** mock `subprocess` for the Claurst parser; benchmark `seed`/`reset`/`grade` with injected
  fake turns (existing `run_turn=` injection pattern).
- **Integration, staged for reliability:** (1) `--agent claurst` over L0→L1→L2 on host Ollama;
  (2) one Aider exercise against the claude baseline, then claurst; (3) one SWE-bench instance in a
  per-test sandbox against the claude baseline, then claurst+Ollama.
- **`ninja check`** (ruff, ruff format --check, mypy, pytest) green at every milestone; the
  in-sandbox install/relay/dep paths exercised by hand (the gate doesn't run them).

## Risks
- **Claurst output format (S2)** may lack structured tool events → side-effect-only grading for that
  AUT.
- **Relay + child-reaping** fragility → readiness-wait inside the exec; one relay per turn.
- **pip flakiness** → offline wheel cache (proven) is the default dep strategy.
- **Claurst maturity** (v0.1.5 beta) → treat as experimental; isolate failures from the claude/
  opencode baselines.
- **SWE-bench per-instance provisioning** is genuinely laborious → start with a tiny curated subset;
  scale only after the path is proven; never silently drop instances.
- **Matrix blow-up** (AUT × suite × tests × models) → gate hard with `enabled`/`select`/`--only`.
