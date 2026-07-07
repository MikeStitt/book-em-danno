# DoR: rename the outer coding-tool concept `agent` → `harness`

## Problem

Danno overloads the word **agent** at two levels:

- **Outer** — the coding *tool* you run a task through (`opencode`, `claude`, `occ`, `claurst`).
  Danno selects, provisions, authenticates, and benchmarks these. Surfaced as `--agent`,
  `benchmarks.toml` `agents = [...]`, `BENCH_AGENTS`, `agent_env`, `run_turn_for(agent=...)`,
  and the "agent-under-test / AUT" framing.
- **Inner** — the LLM *persona/role* a harness runs (`build`, `plan`, …), configured in
  `danno.toml [agents]` and mapped to a model. This is what the tools themselves call an agent.

The same word naming both levels is genuinely confusing — it even collides inside a single file
(`suites/aut.py` imports the inner `DEFAULT_AGENT = "build"` from `sweep` **and** uses the outer
`sb.DEFAULT_AGENT = "opencode"`). This DoR renames the **outer** concept to **harness** and leaves
the **inner** concept as **agent**.

No back-compat alias — danno has no users yet, so this is a clean break. Old `benchmarks.toml`
`agents =` and `--agent` are expected to fail loud after the rename; that is correct.

## The rule

- **harness** = the outer coding tool (`opencode`/`claude`/`occ`/`claurst`).
- **agent** = the inner LLM persona a harness runs (`build`/`plan`/…).

For every `agent` occurrence, ask: does it name a **tool** (→ rename to harness) or a
**persona/role/model-lever** (→ keep agent)?

## MUST-NOT-TOUCH (stays `agent`)

- `danno.toml [agents]` table and everything modelling it: `AgentSpec`, `DannoConfig.agents`,
  `agent_model_name`, `validate`-time `[agents]` validation, in `config/schema.py`.
- **All of `config/generate.py`** (~170 `agent` refs) — opencode.jsonc agent-block generation and
  claurst `AgentDefinition` mapping. Pure inner. Verify no *outer* ref hides here, but do not
  rename the persona code.
- `[sandbox] agent_home` (schema.py) — the inner agent's home directory.
- opencode's own `--agent build` inner flag that danno passes when it *execs* opencode
  (`driver.opencode_run`, `aut.py` opencode branch). Only danno's *own* `--agent` flag is renamed.
- `level0.DEFAULT_AGENT = "build"` — opencode's inner run-agent. **Rename to `DEFAULT_RUN_AGENT`**
  to kill the same-name collision, but it remains conceptually an *agent* (do not call it a harness).

## Core symbol rename map (outer → harness)

| Current (outer) | New | Location |
|---|---|---|
| `DEFAULT_AGENT = "opencode"` | `DEFAULT_HARNESS` | `commands/sandbox.py` |
| `OCC_AGENT = "occ"` | `OCC_HARNESS` | `commands/sandbox.py` |
| `agent_env(agent, …)` | `harness_env(harness, …)` | `commands/sandbox.py` + all callers |
| `provision(…, agent=image)` param | `provision(…, harness=…)` | `commands/sandbox.py` + callers |
| `BENCH_AGENTS` | `BENCH_HARNESSES` | `suites/bench.py` |
| `resolve_bench_agents` | `resolve_bench_harnesses` | `suites/bench.py` |
| `run_bench_agents` | `run_bench_harnesses` | `suites/bench.py` |
| `_agent_dial_ref` | `_harness_dial_ref` | `suites/bench.py` |
| `_variant_cloud_env_lines(agent, …)` param | `(harness, …)` | `suites/bench.py` |
| `BenchOptions.agent` | `BenchOptions.harness` | `suites/bench.py` |
| `run_turn_for(agent, …)` | `run_turn_for(harness, …)` | `suites/aut.py` + callers |
| `resolve_image(agent)` | `resolve_image(harness)` | `suites/aut.py` + callers |
| `install_aut(…, agent, …)` | `install_harness(…, harness, …)` | `suites/aut.py` + callers |
| validate `opts.agent` (`is_occ`/`is_claurst` dispatch) | `opts.harness` | `run.py`, `sweep.py` |
| `benchmarks.toml` `agents = [...]` key | `harnesses = [...]` | `suites/config.py` |
| `BenchmarksConfig.agents` | `BenchmarksConfig.harnesses` | `suites/config.py` |
| `--agent` (bench, repeatable list) | `--harness` | `cli.py` |
| `--agent` (validate, single) | `--harness` | `cli.py` |
| validate/bench options `agent` field | `harness` | wherever the option struct lives |

Keep the tool-id constants `CLAURST` / `OCC` / `CLAUDE` (aut.py) — they name tools; only their
surrounding docstrings shift from "agent" to "harness".

### Prose + identifiers
- "agent-under-test" / "AUT" → "harness-under-test" / "HUT" (~154 hits, mostly docstrings/comments).
- Telemetry (`telemetry/report.py`, `telemetry/provenance.py`): per-agent output subdir
  `<out>/<agent>/` → `<out>/<harness>/`; provenance `agent_versions` → `harness_versions`; report
  column headers "Agent" → "Harness".
- CLI help text and the `# unknown --agent / [agents] name` comments in `cli.py`.

## Execution phases

1. **Core rename** — apply the map above across `src/`. Split the overloaded `DEFAULT_AGENT` first
   (`sandbox.py` → `DEFAULT_HARNESS`; `level0.py` → `DEFAULT_RUN_AGENT`) and fix every import site.
   Then the plumbing in `sandbox.py`, `aut.py`, `bench.py`, `run.py`, `sweep.py`, telemetry, and any
   outer refs in `benchmark.py`, `menu.py`, `registry.py`, `commands/tools.py`, `commands/install.py`,
   `capture/wiring.py`, `level1/level2/base`. Then `cli.py` (`--agent → --harness`), then
   `config.py` (`agents → harnesses`).
2. **Tests + docs** — align `tests/` (~30 files) and docs (`README.md`, `docs/`, `.docs/`, ~23 files)
   to the new names. Update `benchmarks.toml` fixtures/examples using `agents =` → `harnesses =`.
   Leave `[agents]` persona-config tests and docs untouched.
3. **Gate + repair** — `ninja check` (ruff + ruff-format + mypy + pytest -m "not slow"). Fix fallout
   (missed/inconsistent renames, stale references) until green. Pure rename ⇒ green = done.

## Risks / gotchas

- **No blind `sed 's/agent/harness/'`.** It corrupts the inner-persona layer (`[agents]`,
  `AgentSpec`, all of `generate.py`, `agent_home`, opencode's `--agent build`). Classify then rename,
  occurrence by occurrence.
- **The `DEFAULT_AGENT` split is the trap** — two definitions, opposite meanings, cross-imported.
  Resolve it first; the ambiguity that motivated this rename then disappears.
- Renamed symbols cross files — a definition renamed in one module must be updated at every call
  site. This map is the single source of truth so independent editors converge consistently.
- The true rename surface is ~10 core `src/` files + tests + docs — **not** the 170-ref
  `generate.py`, which is inner and stays.
