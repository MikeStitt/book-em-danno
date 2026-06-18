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
- [x] **M0 — danno headless primitives** (capture exec, session-continued run, workspace reset)
- [x] **M1 — Level-0 liveness test + single-config MyST report** (stall oracle, scripted
  conversation runner, MyST page; live-verified flags/schema — see "M1 — DONE" below)
- [x] M2 — config-matrix sweep + results-matrix index (model-axis sweep, guarded
  per-config reset, MyST sweep index; see "M2 — DONE" below)
- [x] M3 — Level-1 tool/bash oracle + tiered sweep (curated deterministic task,
  L0→L1 short-circuit, L1 column/section in the report; live-verified on a fresh
  validator-owned sandbox — see "M3 — DONE" below)
- [x] M4 — Level-2 software-dev oracle (1 small repo+tests task; hidden test suite run
  in-VM, L0→L1→L2 short-circuit, L2 column/section in the report; live-verified — see
  "M4 — DONE" below)
- [x] M5 — Claude Code baseline + comparison row (agent-agnostic `Turn` seam,
  in-sandbox `claude -p` driver, baseline row in the same matrix; live-verified —
  see "M5 — DONE" below)
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

### M0 — DONE (2026-06-17), with these decisions

Implemented on branch `danno-validator-m0` (stacked on `feat-openai-backend-and-provision-fix`):

- **`Runner.capture`** (`src/book_em_danno/core/exec.py`) — a third execution mode beside
  `advise`/`run`: always executes (apply-independent), captures stdout/stderr/exit into a
  `CaptureResult`, `check=False` by default (a stalled/errored AUT turn is *data*, not a
  danno failure). Verbose-only logging — machine-driven, no copy-paste line.
- **`danno_validator.driver`** (new sibling package `src/danno_validator/`) — `capture_exec`,
  `opencode_run` (lenient JSON parse via `OpencodeTurn`; payload fields deliberately
  uninterpreted until the schema is observed live at M1), `seed_workspace`,
  `reset_workspace`.
- **Work-dir + reports → `./.danno-validator/` in the invoking cwd** (gitignored;
  `DEFAULT_WORK_DIR`). Reports land here too — *not* `doc/results/`.
- **Destructive-reset marker guard (mandatory).** The sandbox mounts a host dir into the
  VM, so `git clean -fdx && git reset --hard` mutates the host workspace. `reset_workspace`
  therefore **refuses** (loud `CommandFailedError`) any dir lacking the
  `.danno-validator-workspace` marker that `seed_workspace` drops; `git clean -e` excludes
  the marker so the guard keeps holding across repeated resets. A misconfigured path can
  never wipe a real repo.
- **Packaging.** `danno_validator` ships in the wheel (added to hatch `packages`); its heavy
  deps (Anthropic SDK for the judge, Sphinx/MyST/Jinja2 for reports) go behind the
  `danno[validator]` optional extra (empty at M0). uv stays dev-only; PyPI/pip consumers are
  unaffected.
- **Open for M1:** confirm opencode's session flag (`OPENCODE_SESSION_FLAG = "--session"`,
  from this plan, not from running opencode) against the installed version on the first live
  turn; pin the `-f json` payload schema the stall oracle will read.

### M1 — DONE (2026-06-17), with these live findings

Implemented on branch `danno-validator-m1` (stacked on `danno-validator-m0`). All flags
and the payload schema were **verified live against opencode 1.17.7** in the running
`danno-danno-trials-exp1` sandbox (the opencode-only-in-sandbox invariant held — only the
AUT ran in the VM; the harness ran on the host).

- **Driver corrections vs the plan's assumptions.** The plan said `opencode run -f json`,
  but in 1.17.7 `-f` is `--file` (attach a file) — the structured-output flag is
  **`--format json`** (`OPENCODE_FORMAT_FLAG`). The session flag `--session`/`-s` was
  confirmed correct (`OPENCODE_SESSION_FLAG`). `opencode_run` gained `--agent`, `-m model`,
  and `--dangerously-skip-permissions` (a headless turn must auto-approve or it blocks).
- **`--format json` is JSONL, not one object.** stdout is one JSON event per line,
  interleaved with the occasional human-readable `[time] ERROR …` log block, so
  `parse_events` parses line-by-line and drops non-JSON lines (the error is still
  recoverable from its one-line `{"type":"error",…}` event). `OpencodeTurn` exposes
  `assistant_text`, `tool_calls`/`tool_call_count`, `finish_reason`, `tokens`, `cost`,
  `session_id`, `errors` from the pinned schema: events are `{type, timestamp, sessionID,
  part}`; a **text** event carries `part.text`; a **tool** event (`type=="tool"`) carries
  `part.tool`/`part.callID`/`part.state.status`; a **step_finish** carries `part.reason`,
  `part.tokens`, `part.cost`.
- **Two sandbox gotchas (cost a re-run to find).** (1) The default `run` agent is
  read-only and *refuses* file edits — tool tasks need `--agent build`. (2) The sandbox's
  default cwd `/home/agent/workspace` is an **empty dir**; the workspace is mounted at its
  **verbatim host path**. opencode discovers its project (and `.opencode/opencode.jsonc`,
  hence the configured models) from the exec cwd, so a turn must run with
  `-w <workspace-root>` or it dies with `ProviderModelNotFoundError`. That cwd is also
  opencode's project root, so file writes land where the side-effect probe looks.
- **`oracle.classify_turn`** (pure) tags the failure taxonomy objectively: the L0 **stall**
  = promises action (regex) **and** 0 tool calls **and** no workspace side effect — distinct
  from `refusal`, `hallucinated-tool` (claims it acted), `early-stop`, `malformed-tool-args`,
  `error`. `level0.run_level0` drives the scripted greet→task→nudge conversation over one
  continued session, probes the side effect host-side (the mount is bidirectional), and the
  nudge splits `only-acts-on-nudge` from a fully-`stall`ed model.
- **Reporter** (`report.py`) renders one MyST page per config with stdlib string building
  (no Jinja2/Sphinx yet — a single page needs no template engine; the `danno[validator]`
  extra stays empty until the M2/M6 multi-page toctree). ANSI stripped, raw output fenced.
- **Open for M2:** the matrix sweep needs a **provisioned-per-config** sandbox whose mount
  *is* the validator workspace (its own git repo + generated `.opencode/opencode.jsonc`), so
  `reset_workspace`'s guarded git reset applies and configs are isolated — M1 borrowed the
  exp1 sandbox and only ever touched its single `danno_probe.txt`.

### M2 — DONE (2026-06-17), with these decisions

Implemented on branch `danno-validator-m2` (stacked on `danno-validator-m1`).
`ninja check` green (161 passed). The matrix/sweep split mirrors M1's pure-vs-I/O
discipline: the config expansion and the report renderers are pure and fully
unit-tested; the orchestration is thin and faked in tests.

- **Axis = model (first/default).** `matrix.model_variants(config, only=…)`
  (`matrix.py`) expands one `ConfigVariant` per model **declared** in the base
  danno.toml — the whole catalog, not just agent-assigned models — sorted by danno
  key, driving the L0 battery with that model via OpenCode's `-m <ref>`. `only`
  restricts the sweep and fails loud on an undeclared name. Refs come from the
  generator's resolver, now **public** as `book_em_danno.config.generate.model_ref`
  (was `_model_ref`); an unimplemented backend (llamacpp) or a model missing its
  `tag`/`id` raises at expansion, surfacing a broken base config up front.
- **One validator-owned workspace + one sandbox, opencode.jsonc declares all
  models.** Rather than reprovisioning a sandbox per config, the sweep keeps one
  sandbox whose mount **is** the validator workspace and sweeps models with `-m`.
  This is the cheapest thing that satisfies the M1→M2 prerequisite (a
  validator-seeded workspace so `reset_workspace`'s guard applies) and matches what
  M1 already proved live. Configs that vary *beyond* the model (per-model
  reasoning/context, agent prompts, npm plugins) need a regenerated opencode.jsonc
  (opencode rereads it each `run`, so regenerate-in-place is enough — no
  reprovision) and are a later axis; npm-plugin changes additionally need a sandbox
  restart (deferred).
- **`sweep.prepare_workspace`** seeds the ownership marker, generates the base
  opencode.jsonc, and **commits** it to a fresh git repo — the commit is what lets
  `reset_workspace` (`git clean -fdx -e marker && git reset --hard`) preserve the
  config across runs instead of deleting it as untracked. The seed commit carries an
  inline `-c user.name/email` so it never depends on host git config (CI-safe). This
  was **exercised for real host-side** (no Docker/opencode — invariant preserved):
  prepare → simulate an AUT side effect → run the exact guarded reset → confirmed
  the committed opencode.jsonc survives and the probe file is cleaned.
- **`sweep.run_sweep`** runs the L0 battery against each variant **sequentially**
  (local models are tens of GB resident — no concurrency to win), resetting the
  workspace before each variant via the guarded `reset_workspace` (`reset=False` to
  skip). Provisioning the sandbox itself stays the caller's job.
- **Reporter** (`report.py`) gains `render_matrix_index` + `write_sweep_report`: a
  results-matrix table (config · model · L0 verdict · turns · tokens · latency), a
  failure-taxonomy count summary, and a MyST `{toctree}` whose entries are the
  **actual written page stems**, so index and pages can't drift. Still stdlib
  strings — the `danno[validator]` extra stays empty until the judge (M6) brings the
  Anthropic SDK.
- **Open for M3:** live sweep against a fresh validator-owned sandbox (M1/M2 borrowed
  exp1, whose mount is a real repo so the guarded reset can't run there); the L1
  tool/bash oracle adapter; and the second matrix axis (per-model knobs / prompts)
  via regenerate-in-place.

### M3 — DONE (2026-06-17), with these decisions

Implemented on branch `danno-validator-m3` (branched off the merged `main` — the
whole M0–M2 stack landed, so no more stacking). `ninja check` green (173 passed).
The pure-vs-I/O discipline from M1/M2 holds: the task spec + oracle are pure and
fully unit-tested; the orchestration is thin and faked in tests.

**LIVE-VERIFIED (2026-06-17) on a fresh validator-owned sandbox** — the
carried-forward item from M1/M2 is now closed. Provisioned a throwaway sandbox
(`danno-validator-m3-live`) whose mount *was* a `prepare_workspace`-seeded dir
(`/private/tmp/danno-validator-m3-live`: marker + generated opencode.jsonc +
committed git repo), then `run_sweep`'d the trials `danno.toml` over two models.
Result matrix:

    config        L0 verdict   L1 verdict
    gemma3-27b    error        —  (skipped)
    gpt-oss-20b   pass         pass

This exercises the whole M3 path live: `gemma3:27b` (no tool support) errors at L0
so **L1 short-circuits** (the tiering); `gpt-oss:20b` passes L0, then the L1
line-count task elicits real tool use and the deterministic oracle confirms
`line_count.txt == "7"` → pass. The guarded `reset_workspace` ran cleanly between
variants (the committed opencode.jsonc survived; probe/seed files cleaned), and the
report rendered the L1 column (`—` for the skipped config) and the per-config
`## Level 1 — tool/bash` section. Sandbox removed afterward. (Driver: a scratch
orchestration script, gitignored — promote to a `danno_validator` entry point if the
sweep CLI is built later.)

- **L1 reuses the L0 oracle — no new failure class.** `level1.run_level1` drives
  one headless turn (`--agent build -w <ws> --dangerously-skip-permissions`, via
  the M1-verified `driver.opencode_run`), computes the deterministic side effect,
  and feeds it into the *same* pure `oracle.classify_turn(side_effect=…,
  expects_action=True)`. So an L1 result lands in the existing `FailureClass`
  taxonomy automatically: a clean tool call that produced the **wrong** content is
  `early-stop` (tool ran, required change absent); a tool that errored is
  `malformed-tool-args`; talk-but-no-act is `stall`/`hallucinated-tool`. No
  L1-only class was needed, exactly as the plan anticipated.
- **Tasks are declarative; the oracle is a file comparison.** `Level1Task`
  (`label`, `prompt`, `inputs` = `(name, content)` pairs, `output_file`,
  `expected_output`) makes the oracle a pure stripped-content equality check — the
  L1 "no LLM judge" contract — and trivially unit-testable. `seed` is **surgical**:
  it writes the inputs and unlinks only its own expected output (so a stale correct
  output can't fake a pass, mirroring L0's probe reset) — it never runs a
  destructive git reset, so L1 needs no extra `reset_workspace` between L0 and L1.
- **Curated default = a bash line-count task.** Seeds `data.txt` (7 known lines),
  asks the agent to count lines *with a shell command* and write the digits to
  `line_count.txt`; the oracle checks the file equals `"7"`. Chosen because the
  answer is a single deterministic integer **and** producing it genuinely requires
  tool/bash use (a pure "echo this literal string" task wouldn't exercise tools).
  A larger task bank / `--full` and the general benchmark-adapter path
  (Terminal-Bench, InterCode-Bash) are deferred, per "start curated".
- **Tiered sweep with a short-circuit.** `sweep.SweepResult` gains
  `level1: TaskResult | None`; `run_sweep` gains `level1: bool = True` and, for each
  variant that **passes L0**, runs L1 against the same workspace. A config that
  fails L0 skips L1 (`level1` stays `None`) — the plan's tiering, so a stalling
  model never wastes a run on L1. No reset between L0 and L1 (the task seeds its own
  clean state surgically); the per-variant guarded reset still isolates configs.
- **Reporter** gains an **L1 verdict column** in the results matrix (`—` when L1
  was skipped, which reads as "L0 didn't pass") and an appended **`## Level 1 —
  tool/bash`** section on each config page (verdict, reply, tool calls, side-effect
  flag). Still stdlib strings — the `danno[validator]` extra stays empty until the
  judge (M6). The L0 transcript heading became `## Level 0 — liveness` so the two
  tiers read as parallel sections on one page.
- **Open for M4:** the Level-2 dev oracle (one small repo+tests task with a hidden
  test suite as oracle); and the second matrix axis (per-model knobs / prompts) via
  regenerate-in-place. (The live-sweep prerequisite is now closed — see
  "LIVE-VERIFIED" above.) A larger L1 task bank / `--full` and the general
  benchmark-adapter path (Terminal-Bench, InterCode-Bash) also remain deferred.

### M4 — DONE (2026-06-17), with these decisions

Implemented on branch `danno-validator-m4` (branched off the merged `main` — the
whole M0–M3 stack landed). `ninja check` green (187 passed). The pure-vs-I/O
discipline from M1–M3 holds: the task spec + the verdict mapping are pure and
fully unit-tested; the orchestration (seed → drive → run the suite in-VM) is thin
and faked in tests.

**LIVE-VERIFIED (2026-06-17) on a fresh validator-owned sandbox**
(`danno-validator-m4-live`, mount `/private/tmp/danno-validator-m4-live`: a
`prepare_workspace`-seeded marker + generated opencode.jsonc + committed git repo),
sweeping the trials `danno.toml` over two models with the full L0→L1→L2 chain. The
sandbox VM was first probed for the test runtime — `python3` is present (**Python
3.14.4**; `node` v22 too), so the curated Python suite runs as-is. Result matrix:

    config        L0 verdict   L1 verdict   L2 verdict
    gemma3-27b    error        —  (skip)    —  (skip)
    gpt-oss-20b   pass         pass         early-stop (hidden tests exit 1)

`gemma3:27b` (no tool support) errors at L0 so **both higher tiers short-circuit**;
`gpt-oss:20b` passes L0 and the L1 line-count, then reaches the L2 fizzbuzz task —
the hidden test suite is written in only at grading time and **run inside the VM**
(`python3 hidden_test_fizzbuzz.py`), and its exit code is the objective verdict.
(Driver: `scratch/m4_live_sweep.py`, gitignored — promote to a `danno_validator`
entry point if the sweep CLI is built later.)

**The live L2 result is itself the strongest validation of the oracle.**
`gpt-oss:20b` *printed a correct fizzbuzz in its reply text* but made only
`glob`/`bash`/`read` tool calls — **no write/edit** — so the on-disk `fizzbuzz.py`
still held the stub and the hidden suite hit `raise NotImplementedError` (exit 1 →
`early-stop`). That is a live instance of the exact *promised-but-didn't-act*
failure this harness exists to catch, and it proves the hidden-test oracle grades
the **workspace, not the model's claims**: a model that shows correct code it never
saved still fails. (`early-stop` rather than `hallucinated-tool` because the model
did make real tool calls — it just stopped before the required edit landed; the
hallucinated-tool class is reserved for *zero* tool calls.)

- **L2 reuses the L0/L1 oracle — no new failure class.** `level2.run_level2` drives
  one headless turn (`--agent build -w <ws> --dangerously-skip-permissions`, via the
  M1-verified `driver.opencode_run`), runs the hidden suite in the VM, and feeds the
  pass/fail boolean into the *same* pure `oracle.classify_turn(side_effect=…,
  expects_action=True)`. So an L2 result lands in the existing `FailureClass`
  taxonomy automatically: tests pass = `pass`; a clean edit that still fails the
  suite = `early-stop`; a tool error = `malformed-tool-args`; talk-but-no-edit =
  `stall`/`hallucinated-tool`. No L2-only class was needed (M3's discipline). The
  richer record (the captured `TestRun`) lives on `DevTaskResult`, not the taxonomy.
- **The oracle is a hidden test suite, run IN the sandbox.** Unlike L1's host-side
  file compare, the L2 oracle is the *test run itself* — and the repo lives in the
  mounted workspace with the VM's toolchain, so the suite runs in-VM via
  `driver.capture_exec` (the opencode-only-in-sandbox invariant covers the
  agent-under-test; the test run is the oracle and belongs in the VM too). The host
  harness only writes the test in and reads the exit code. **Exit 0 = pass** is the
  one convention `test_command` must follow, so the same machinery works for
  `python3 t.py`, `node t.js`, or `pytest`.
- **The test is genuinely hidden.** `Level2Task.seed` writes only the source stub and
  **removes** any `test_file`, so the agent's turn runs against a repo with no test
  to read for hints or hardcode against; `run_tests` writes the suite in only at
  grading time. Surgical like L1's seed (no destructive git reset), so no extra
  `reset_workspace` is needed between L1 and L2 — the per-variant guarded reset still
  isolates configs.
- **Curated default = implement FizzBuzz.** The seeded `fizzbuzz.py` stub raises, so
  nothing passes until the agent writes the real logic — a genuine source edit (not a
  one-literal task) with a fully specified, deterministic contract the hidden suite
  checks exactly (12 cases). Chosen over full SWE-bench deliberately: SWE-bench's
  per-task docker harness would mean nested virtualization inside the sandbox VM. A
  larger bank / `--full` and the Aider-polyglot / Exercism repo+tests adapter path
  are deferred.
- **Fail-loud on a missing runtime.** A `test_command` exit of **127** means the test
  interpreter is absent from the image (a harness misconfiguration), so `run_tests`
  raises `CommandFailedError` rather than silently scoring every model as failing the
  suite (Working Rule 8). The pre-run VM probe (python3 present) is the cheap check
  that keeps this from firing.
- **Tiered sweep extended.** `SweepResult` gains `level2: DevTaskResult | None`;
  `run_sweep` gains `level2: bool = True` and runs L2 **only when L1 passed**
  (`l1 is not None and l1.passed`) — completing the L0→L1→L2 short-circuit chain (a
  config that fails any tier skips all later ones, their fields staying `None`).
- **Reporter** gains an **L2 verdict column** in the results matrix (`—` when L2 was
  skipped) and an appended **`## Level 2 — software dev`** section on each config page
  (verdict, reply, tool calls, hidden-test pass/fail + command/exit, and the fenced
  test output). Index title is now "(L0 + L1 + L2)". Still stdlib strings — the
  `danno[validator]` extra stays empty until the judge (M6).
- **Open for M5:** the Claude Code baseline + comparison row (same battery vs the
  `claude` agent headless, normalised into the same result record). The second matrix
  axis (per-model knobs / prompts) via regenerate-in-place, a larger L2 task bank /
  `--full`, and the general benchmark-adapter path also remain deferred.

### M5 — DONE (2026-06-17), with these decisions

Implemented on branch `danno-validator-m5` (branched off the merged `main` — the
whole M0–M4 stack landed). `ninja check` green (202 passed). The pure-vs-I/O
discipline holds: the new driver's parsing and the baseline wiring are pure and
fully unit-tested; the orchestration is thin and faked in tests.

**LIVE-VERIFIED (2026-06-17)** on fresh validator-owned sandboxes over one shared
`prepare_workspace`-seeded mount (`/private/tmp/danno-validator-m5-live`): an
**opencode** sandbox swept `gpt-oss-20b` and a **claude** sandbox ran the Claude
Code baseline, combined into one report. Result matrix:

    config        L0 verdict   L1 verdict   L2 verdict   turns  tokens   latency
    gpt-oss-20b   pass         pass         pass            2   19056    122.1s
    claude-code   pass         pass         pass            2     152     12.5s   (baseline)

This proves the agent-agnostic comparison: **the same oracles graded both agents**
(L0 probe file, L1 `line_count.txt == "7"`, L2 hidden fizzbuzz suite run in-VM →
"ok — 12 cases passed", exit 0). Claude drove real tool use (L1 `Bash`+`Write`, L2
`Read`+`Edit`) and the matrix surfaces a real datapoint the harness exists to
capture — claude reaches the same oracle outcomes at ~125× fewer tokens and ~10×
lower latency than the local model on these tasks.

- **One agent-agnostic seam, two transcript formats.** A structural `Turn`
  protocol (`driver.py`) captures exactly the read surface the oracle, the level
  runners, and the reporter consume (`assistant_text`, `tool_calls`,
  `tool_call_count`, `session_id`, `tokens`, `cost`, `errors`, `error_summary`);
  both `OpencodeTurn` and the new `ClaudeTurn` satisfy it with no inheritance. The
  level runners gained an injectable `run_turn: TurnFn` (resolved at call time so
  existing monkeypatched-`opencode_run` tests still hold), and the per-variant
  L0→L1→L2 short-circuit was extracted from `run_sweep` into a shared
  `sweep.run_tiers` that both the model sweep (`run_turn=opencode_run`) and the
  baseline (`run_turn=claude_run`) call. No oracle/report behavior changed.
- **In-sandbox `claude -p`, no new deps.** `driver.claude_run` drives
  `docker sandbox exec … claude -p --output-format stream-json --verbose`
  (+`--resume`/`--dangerously-skip-permissions`), `ClaudeTurn` parses the JSONL
  onto the `Turn` surface, and tool errors map from `tool_result.is_error`. Flags
  AND the stream-json schema were **pinned live against claude 2.1.179** (M1
  discipline) — this time the plan's assumptions held (`-p`,
  `--output-format stream-json`, `--verbose`, `-r/--resume`,
  `--dangerously-skip-permissions`, `--model` all confirmed; no `-f`-style
  surprise). The baseline is driven via the CLI in a claude sandbox, so the
  `danno[validator]` extra stays empty (the Anthropic SDK still waits for M6's judge).
- **Pin AND track the claude model (like opencode's `-m`).** The default model
  varies wildly in cost/latency/behaviour, so the baseline does not ride it:
  `run_baseline(model=…)` pins `claude --model <alias|id>` (control), and the row
  records the model claude **actually resolved** — `ClaudeTurn.model` reads it from
  the `system` init event, so the matrix shows the real model (e.g.
  `claude-opus-4-8[1m]`) even when unpinned (track). The bound model rides the same
  `TurnFn` wrapper as the auth file, kept out of the agent-agnostic runner API.
- **The baseline is just another `SweepResult` row.** `baseline.run_baseline`
  returns one `SweepResult` carrying a synthetic `claude-code` variant
  (`BASELINE_MODEL`), so appending it to a sweep renders it as a matrix row + page
  for free. The reporter flags it (`_(baseline)_`) and excludes it from the
  swept-config tally and the failure-taxonomy counts — those describe the models
  under test; the baseline is the reference.
- **Auth was the one real gap (caught only live).** claude needs
  `CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY` in its exec env, but danno injects
  auth via `--env-file` only on interactive `launch`; a bare `docker sandbox exec`
  inherits none, so the first live run scored every claude turn `error`
  ("Not logged in"). Fix: `claude_run` accepts `env_file` → `--env-file`, and
  `run_baseline` builds a chmod-600 auth env-file (reusing danno's `agent_env`
  fail-loud + `_build_env_file`), binds it via a `TurnFn` wrapper kept out of the
  agent-agnostic runner API, and removes it after. opencode is unaffected (it
  reads Ollama from `opencode.jsonc`, not env) — exactly why the gap hid until the
  claude path ran live. The host token is supplied out-of-band (`claude
  setup-token` → exported), never committed.
- **Open for M6:** the annotated "menu" danno.toml emitter, and the
  Anthropic-SDK judge for fuzzy partial-credit on top of the objective oracles.
  The second matrix axis (per-model knobs / prompts), larger task banks / `--full`,
  and the general benchmark-adapter path remain deferred. (Live driver:
  `scratch/m5_live_baseline.py`, gitignored — promote to a `danno_validator` entry
  point if the sweep CLI is built later.)

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
