# Plan: `danno-validator` — find which danno.toml configs actually work

> **Living document.** Design/plan for a validation harness that sweeps
> `danno.toml` configurations, runs a tiered test battery against the sandboxed
> agent, judges results with an administrator AI, and emits a Sphinx report plus an
> annotated "menu" `danno.toml`. `.docs/` is **exempt from markdown/format checks**.
> Companion: [`connecting-claude-code-to-models-investigation.md`](connecting-claude-code-to-models-investigation.md)
> (the protocol/headless/fidelity background this builds on). Verify version-specific
> claims (opencode/claude headless flags, benchmark availability) before relying.

## Status

Legend: `[ ]` todo · `[~]` in progress · `[x]` done

- [x] Background investigation (headless modes, tool-use fidelity, native endpoints)
- [ ] **M0 — danno headless primitives** (capture exec, session-continued run, workspace reset)
- [ ] M1 — Level-0 liveness test + single-config MyST report
- [ ] M2 — config-matrix sweep + results-matrix index
- [ ] M3 — Level-1 tool/bash oracle (pull 1 benchmark task)
- [ ] M4 — Level-2 software-dev oracle (1 small repo+tests task)
- [ ] M5 — Claude Code baseline + comparison row
- [ ] M6 — annotated "menu" danno.toml emitter
- [ ] M7 — serve+SDK rich backend · llama.cpp model switching · full benchmark banks

## Goal

Given a *space* of danno.toml configurations — permutations of **model × prompts/agents
× tools/plugins** — `danno-validator` provisions each in the Docker sandbox, runs a
**tiered test battery**, scores it (objective oracles + an administrator AI), and produces:

1. a **Sphinx report** (per-config pages + a results matrix, incl. a Claude Code
   baseline row), and
2. a **large annotated `danno.toml`** ("menu" file) listing every candidate block with
   its validation verdict in comments — the user **comments/uncomments** to assemble a
   working final config.

This is the constitution's *Configuration-is-Code* rule mechanized: it doesn't just
generate configs, it **exercises** them and reports what converges. The opencode-only-
in-sandbox invariant is preserved — the **agent-under-test (AUT) runs only in the VM**;
the harness and judge run on the host.

## Test tiers (default: 1 test per level)

The defining failure we're chasing (observed on `gemma3:27b`): the agent **says it will
act, makes no tool call, stops, and waits** — a *promised-but-didn't-act* stall. The
battery is tiered so a config that fails Level 0 never wastes time on Level 2.

- **Level 0 — Liveness / conversation.** A short *scripted multi-turn* session
  (greet → task that needs a tool → "please proceed" nudge). **Objective stall signal:**
  response text promises action **and** 0 tool calls **and** no workspace side effect →
  `stall`. The administrator AI judges coherence/appropriateness on top. The nudge turn
  classifies *only-acts-on-nudge* vs *fully-stalled*.
- **Level 1 — Tool / bash use.** Tasks with **verifiable side effects** (create
  `foo.txt` with content X; run a command and report its output) → deterministic oracle,
  no LLM judge needed.
- **Level 2 — Software development.** A real task with a **hidden test suite as oracle**
  (apply change → run tests → pass/fail). AI judge only for partial credit / quality.

Default run = **one task per level** (a fast gate); `--full` runs the whole bank.

## Pull from existing benchmarks (adapter layer)

The validator wraps existing suites via small **adapters** that map a benchmark task →
`(workspace seed, prompt, oracle)`. Candidate sources, by level:

- **Level 1 (tool/bash):** Terminal-Bench, InterCode-Bash, or a curated handful of
  side-effect tasks. (Function-call leaderboards like BFCL are API-level, not agentic-
  in-repo — less relevant.)
- **Level 2 (dev):** **Aider polyglot / Exercism** style (a repo + tests, *no docker*)
  or a tiny curated repo+pytest task as the **default** — deliberately **not** full
  SWE-bench, whose per-task docker harness means **nested virtualization inside the
  sandbox VM** (heavy/fragile). SWE-bench(-Lite/Verified) can be a later `--full` adapter
  if run host-side rather than in-VM.
- **Level 0:** no external bench needed — a small built-in scripted conversation bank.

Adapters normalize licensing/size; defaults pick the smallest viable task each.

## Architecture (host-side harness)

A Python package (`danno_validator`, sibling to `book_em_danno`, reusing its config
loader/generator). Components:

1. **Matrix generator** — from a `validation-matrix.toml` (or CLI flags), expand N
   `danno.toml` variants (vary model / agent prompts / tools / npm plugins). Reuses
   `book_em_danno.config` (`render_config`, schema).
2. **Provisioner** — per permutation: generate `opencode.jsonc`, provision the sandbox
   (or reconfigure in place), inject model keys (the `{env:VAR}` fail-loud check),
   **reset the mounted workspace** (`git clean -fdx && git reset --hard`).
3. **Driver** — run the battery against the AUT (see "Driving the AUT" below); capture
   per-turn **events**: messages, tool calls, finish reason, side effects, latency,
   tokens/cost.
4. **Oracles** — per level: stall-detector (L0), side-effect checker (L1), test-suite
   runner (L2).
5. **Administrator AI (judge + L0 conversation driver)** — Claude API
   (opus/sonnet/haiku, configurable) using the user's key. Objective oracles are the
   backbone; the judge handles fuzzy grading and L0 appropriateness.
6. **Reporter** — MyST pages + toctree matrix + the annotated "menu" danno.toml.

## Driving the AUT (resolves the networking wrinkle)

`opencode serve` binds a port **inside the VM**, and `docker sandbox` has **no port
publish** (`create` only takes workspace mounts). So host→serve isn't directly reachable.
Two backends, defaulting to the portable one:

- **Default — session-continued `opencode run -f json` via captured `exec`.** Each turn
  is `docker sandbox exec <name> opencode run -f json --session <id> "<prompt>"` (no
  `-it`), stdout captured on the host. Multi-turn via the same session id. Fully host-
  driven, **no port exposure needed**. Sufficient for L0/L1/L2 because side effects land
  in the mounted workspace (host-readable) and `-f json` carries tool-call/finish info.
- **Rich (M7) — `opencode serve` + `@opencode-ai/sdk`.** For full event streaming
  (per-token, intermediate tool events), run a **small in-VM driver script** (talks to
  `localhost:<serve>` inside the VM) that writes a JSON results file into the **mounted
  workspace**; the host reads it. This keeps everything in-VM/loopback — no host port,
  no allow-host rule.

> Note the tension with the WAL fix: opencode's full transcript DB is **VM-local**, so
> the harness relies on `-f json` / the in-VM driver's JSON, **not** on reading the data
> dir from the host.

## danno additions required (M0 prerequisites)

These are the only true blockers; everything else is harness code.

- **Capture exec primitive** — a non-interactive `Runner`/`exec_in_container` variant
  that runs `docker sandbox exec <name> …` (no `-it`) with `capture_output=True` and
  returns stdout/stderr/exit. (Current `run`/`advise` stream to the terminal.)
- **Session-continued run helper** — issue an `opencode run -f json` turn against a named
  session in a provisioned sandbox.
- **Workspace reset** — `git clean/reset` the mount between runs (idempotent battery).
- **Programmatic config matrix** — thin override/template layer over `render_config`
  (no schema change; just generate many configs).
- (M7) **headless `opencode serve` launch** + the in-VM driver bridge.

These can live in `danno_validator` calling `book_em_danno` library functions, keeping
the `danno` CLI surface small; promote into the CLI only if broadly useful.

## The annotated "menu" danno.toml

The signature deliverable. The emitter writes a large `danno.toml` where every candidate
block carries its verdict as a comment, e.g.:

```toml
# [L0 ✓ · L1 ✓ · L2 ✗ stalls on multi-step edits]  — verified 2026-06-17
[models.gemma3-27b]
backend = "ollama"
tag     = "gemma3:27b"
...

# [L0 ✓ · L1 ✓ · L2 ✓]  RECOMMENDED for coder/build
[models.qwen3p6-27b]
...

[agents]
plan  = "qwen3p6-27b"     # [L0 ✓ L1 ✓ L2 ✓]
# plan = "gemma3-27b"     # [L2 ✗] — uncomment to use, fails dev tasks
```

Because TOML can't hold two values for one key, "comment/uncomment" applies to the
**agent→model assignments** (and optional tool/plugin lines): all candidate `[models.*]`
are present; the user picks assignments by uncommenting. Verdicts are comments only.

## Sphinx report

Repo already uses Sphinx; add **MyST-Markdown** (less escaping pain than rST for
transcripts). Generated via Jinja2 → `doc/results/` → `sphinx-build`.

- **Per-config page:** config hash, model/prompts/tools, `opencode.jsonc` excerpt; per-
  level transcript, objective signals (tool-call count, finish reason, side-effect
  checks), judge verdict + rationale, latency/tokens/cost, pass/fail.
- **Index (toctree):** the **results matrix** — rows = configs (incl. the Claude Code
  baseline), cols = levels, cells = pass/fail/score — plus a **failure-taxonomy** summary.
- Hygiene: strip ANSI, wrap raw model output in fenced/literal blocks.

## Failure taxonomy (recorded per run)

`stall (promised-no-act)` · `only-acts-on-nudge` · `loop` · `malformed-tool-args` ·
`early-stop` · `refusal` · `hallucinated-tool` · `pass`. Both the objective oracle and
the judge tag a class; the report aggregates counts.

## Claude Code baseline (M5)

Same battery vs the `claude` agent headless (`claude -p --output-format json` / Agent
SDK), normalized into the **same result record** (final answer, tool calls, files
changed, tests passed, latency, tokens, class). Comparison is on **agent-agnostic oracle
outcomes** (workspace side effects + tests), sidestepping transcript-format differences.

## Risks / open questions

- **docker sandbox port exposure** — mitigated by the exec+`run -f json` default and the
  in-VM driver for serve (M7).
- **RAM / concurrency** — local models are huge (Nemotron ≈31.5 GB); sweeps over local
  models run **sequentially**. Cloud models parallelize (rate limits aside).
- **Cold-start latency** — `[[npm]]` plugins install per fresh VM; amortize via
  `opencode serve` attach or a baked template (`docker sandbox save`).
- **Benchmark fit** — avoid SWE-bench's nested-docker harness for the in-VM default;
  prefer repo+tests tasks.
- **Judge reliability/cost** — keep objective oracles primary; judge for fuzzy only;
  record judge model + cost per run.
- **Determinism** — N trials per config, report pass-rate; pin temperature/seed where
  the model allows.

## Milestones (de-risking order)

- **M0** danno headless primitives (capture exec, session-continued run, workspace reset).
- **M1** Level-0 liveness against one config → one MyST page. Prove the stall detector
  flags `gemma3:27b` and passes a known-good model.
- **M2** matrix generator + sequential sweep + results-matrix index.
- **M3** Level-1 tool/bash oracle (one pulled benchmark task).
- **M4** Level-2 dev oracle (one small repo+tests task).
- **M5** Claude Code baseline + comparison row.
- **M6** annotated "menu" danno.toml emitter.
- **M7** serve+SDK rich backend; llama.cpp model switching; full benchmark banks.

## Relationship to current work

Builds on the merged/in-flight sandbox fixes: the `{env:VAR}` fail-loud check, the
`openai` backend kind, the provision idempotency fix, and the `--` passthrough — all on
`feat-openai-backend-and-provision-fix` / `fix-claude-sandbox-trust-prompt`. M0's capture
primitive is the natural next danno addition.
