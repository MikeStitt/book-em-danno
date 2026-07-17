# DoR: A formal Harness API + self-discovering registry

Drop **occ**, convert **opencode**/**claurst** onto the API, add **codex**, keep
**claude** as the (registered-but-unique) reference harness, and close issue
[#97](https://github.com/MikeStitt/book-em-danno/issues/97).

Status: **DESIGNED, not started.** Delivery is a **staged PR stack** (Phase 1 →
2 → 3), each gate-green (`ninja check`) before the next. Slow suite
(`uv run pytest -m slow`) re-run at the end of Phase 3.

---

## 1. Problem

A "harness" (the outer coding tool danno drives — opencode / claude / occ /
claurst) is passed around as a **bare `str`**. There is **no** `Harness`
class/ABC/Protocol. The abstraction is a set of `str`-keyed `if harness == …`
chains plus a duck-typed `Turn`/`TurnFn` Protocol on the transcript side. The
name-set is re-declared in ≥6 places and every concern (install, config-gen,
env, model-resolution, cloud-auth, capture, launch, telemetry, matrix
membership) is a scattered branch. Adding **codex** the current way means editing
~a dozen files and touching every one of those chains; there is no single place
that says "everything codex-specific lives here."

Goal: **one module per harness holds all of that harness's unique code**, and the
rest of the system **self-discovers** the registered harnesses instead of
hardcoding the name-set and branching on it.

### The name-set / dispatch surface today (single source of truth for this work)

Redundant name declarations:
`suites/config.py:25` (`HarnessName` Literal) · `suites/aut.py:19-21` ·
`suites/bench.py:108` (`BENCH_HARNESSES`) · `commands/sandbox.py:53,60,69` ·
`config/schema.py:30-31` (`Overrides` closed keys) · `run.py:52,320-321` · CLI
help `cli.py:152,270,446,521`.

`if harness == …` dispatch sites (every place behavior forks):
- `suites/aut.py` — `resolve_image` / `install_harness` / `run_turn_for` (the
  three cleanest existing seams).
- `commands/sandbox.py` — `_docker_image`, `provision`, `harness_env`,
  `resolve_model_for_harness`, `resolve_start`, `_exec_session`, `launch`,
  `_ensure_provisioned`, `_capture_session`, `update` (~12 sites).
- `suites/bench.py` — `resolve_bench_harnesses`, `_variant_cloud_env_lines`,
  `_harness_dial_ref`, `_build_bench_env_files`, `_setup_bench_capture`,
  `_seed_opencode_config`, matrix branch (`_openai_compat_variants` vs
  `_claude_inert_models`), gate/steps.
- `run.py:320-351` — validate sweep image/install/turn selection.
- `config/generate.py` — `_override(overrides, harness)` keyed by `"opencode"` /
  `"claurst"` throughout; `generate` (opencode.jsonc) vs `generate_claurst_*`.
- `telemetry/provenance.py:57-63` (version pins) · `telemetry/report.py:529`
  (`harness == "claude"`) · `benchmark.py:196` (non-opencode rejection) ·
  `suites/base.py:165,190` (reap/survivor process-name patterns).

Existing abstractions (the only ones, both kept): `Turn` Protocol
(`driver.py:248`) and `TurnFn` Protocol (`driver.py:277`).

---

## 2. Design

### 2.1 Two harness *kinds* (matches the user's framing)

- **Dialer** — danno points it at an endpoint it controls (Ollama / an
  OpenAI-compatible cloud): **opencode, claurst, codex** (and occ, being
  deleted). They share the matrix, cloud-auth, dial-ref, and capture machinery.
  **This is the contract to standardize.**
- **Reference** — carries its own endpoint + auth and selects by native
  `--model` over **inert-backend** models: **claude**. It is a **registered**
  harness (so dispatch is uniform) but declares `kind = REFERENCE` and
  implements a partial contract (no danno-dialed endpoint, no capture). This is
  how claude "stays unique" without a special-case `str` check everywhere.

### 2.2 The `Harness` contract

A `Harness` is a **dataclass/Protocol** (a value object, not a god-class); the
per-turn transcript stays the existing `Turn`/`TurnFn` seam. Home:
**`src/danno_validator/harnesses/`** (danno_validator already depends on
book_em_danno; book_em_danno already does local imports back into danno_validator
to avoid cycles — we keep that direction).

```python
class WireProtocol(StrEnum):        # capability that routes capture/metrics/#97
    CHAT = "chat"                   # OpenAI chat-completions: opencode(/v1 openai), claurst→relay
    RESPONSES = "responses"         # OpenAI Responses API: codex (and opencode's @ai-sdk openai path)
    ANTHROPIC = "anthropic"         # claude → api.anthropic.com

class HarnessKind(StrEnum):
    DIALER = "dialer"
    REFERENCE = "reference"

@dataclass(frozen=True)
class Harness:
    name: str
    kind: HarnessKind
    sandbox_image: str                 # prebuilt image name, or "shell"
    wire_protocol: WireProtocol
    supports_capture: bool
    reap_patterns: tuple[str, ...]     # bracketed process-name patterns (suites/base.py)
    overrides_key: str | None          # generate.py override application key, or None

    # provisioning (book_em_danno seam; no-op where N/A)
    install: Callable[[Runner, str, DannoConfig | None], None]
    harness_env: Callable[[str, Path | None], list[str]]        # (ollama_url, home) -> env lines
    generate_config: Callable[..., None]                        # opencode.jsonc / codex config.toml / claurst models.json; no-op if none
    interactive_launch_script: Callable[..., list[str]]         # (model_ref, passthru, *, capture_port) -> container_argv

    # driving (danno_validator seam)
    turn_fn: Callable[..., TurnFn]                              # (env_file, *, capture_port, model_override, max_turns) -> TurnFn

    # model / cloud resolution (DIALER: real; REFERENCE: --model semantics)
    resolve_model: Callable[[DannoConfig, str], str]
    cloud_env_lines: Callable[[DannoConfig, str], list[str]]
    dial_ref: Callable[[DannoConfig, ConfigVariant], str | None]
    model_matrix: Callable[[DannoConfig, Sequence[str] | None], list[ConfigVariant]]

    # telemetry
    provenance: Callable[..., dict]
```

Each harness module owns its **`*Turn` dataclass** (the stream-json / event
parser — moved out of `driver.py`) plus the functions bound into the `Harness`
value. `driver.py` keeps only the **shared** seam: `Turn`/`TurnFn` Protocols,
`capture_exec`, `parse_events`, `reset_workspace`, and the shared Ollama relay
(`_OLLAMA_RELAY_SOURCE`, `_claurst_script` → rename to `_ollama_relay_script`).

### 2.3 The registry (decorator + package imports)

```python
# danno_validator/harnesses/__init__.py
_REGISTRY: dict[str, Harness] = {}

def register(h: Harness) -> Harness:
    if h.name in _REGISTRY:                       # fail loud (Working Rule 8)
        raise ValueError(f"duplicate harness '{h.name}'")
    _REGISTRY[h.name] = h
    return h

def get(name: str) -> Harness:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown harness '{name}'. Valid: {', '.join(all_names())}")

def all_names() -> tuple[str, ...]:
    return tuple(_REGISTRY)                        # registration order

# import each submodule so importing the package self-populates the registry:
from . import opencode, claurst, claude, codex     # noqa: E402,F401
```

Each submodule ends with `register(Harness(name="codex", …))`. **Adding a
harness = new module + one import line.** Every dispatch site above becomes a
registry lookup:

- `resolve_image(h)` → `get(h).sandbox_image`
- `install_harness(...)` → `get(h).install(...)`
- `run_turn_for(...)` → `get(h).turn_fn(...)`
- `BENCH_HARNESSES` / `HarnessName` validation → `all_names()`
- bench matrix → `get(h).model_matrix(config, only)` (dialer = openai-compat
  minus inert; reference = inert models / baseline row)
- `_variant_cloud_env_lines` → `get(h).cloud_env_lines(...)`
- `_harness_dial_ref` → `get(h).dial_ref(...)`
- `harness_env` / `provision` / `launch` / `update` in sandbox.py → local-import
  the registry and delegate.
- reap/survivor patterns → union of `get(h).reap_patterns`.
- `generate.py` override key → `get(h).overrides_key`.

### 2.4 Config-schema self-discovery

`suites/config.py` is in danno_validator → replace the `HarnessName` `Literal`
with a `field_validator` calling `harnesses.all_names()` (fail loud on unknown;
names the valid set). `benchmarks.toml [harnesses]` and `[gates.harness.<name>]`
then validate against the live registry — no name-set edit to add a harness.

`config/schema.py` (`Overrides` closed keys) is in **book_em_danno**; importing
the danno_validator registry at module-load risks a cycle. Resolve by a
**deferred local import inside the validator function** (the pattern sandbox.py
already uses), so the closed override-key set is `{h.name for h in registry if
h.overrides_key}`. If that proves awkward, fall back to a tiny hand-kept tuple in
book_em_danno with a **unit test asserting it equals the registry's
override-capable set** (fail-loud drift guard). Decide during Phase 1;
default to the deferred import.

---

## 3. Codex specifics (the new harness)

**Codex speaks ONLY the OpenAI Responses API** (`/v1/responses`);
`wire_api="chat"` was *removed* from the Rust source (~Feb 2026), including the
built-in `ollama`/`lmstudio` providers. Consequences baked into the design:

- `wire_protocol = RESPONSES`. Local Ollama requires **Ollama ≥ 0.13.3** (first
  version exposing `/v1/responses`, experimental + non-stateful) — a **doctor
  check** + a loud bench pre-flight.
- **Install:** `npm install -g @openai/codex` (proxy-aware, like other npm/curl
  installs). Image = `shell`. Stamp + skip-guard mirroring claurst/occ.
- **Config-gen:** `generate_codex_config` → `$CODEX_HOME/config.toml` with
  `[model_providers.<id>] base_url wire_api="responses"` (+ `env_key` for cloud;
  omit for local no-auth). `CODEX_HOME` set per-session to a VM-local dir (like
  claurst's `CLAURST_MODELS_PATH`). Alternatively drive fully via repeated
  `-c model_providers.…=…` flags (no file) — pick the file form for parity with
  the other config-gen harnesses; decide in the Phase-0 spike.
- **Drive (headless):**
  `codex exec --json -a never -s danger-full-access --skip-git-repo-check
  -C <workspace> -m <tag> "<prompt>"` (autonomous, no prompts; outer Docker
  sandbox is the isolation boundary, so `danger-full-access` is acceptable —
  same rationale as occ's `bypassPermissions`). `--json` → NDJSON events
  (`thread.started` / `item.*` / `turn.completed`) → a new `CodexTurn` parser.
- **Base-URL routing / proxy reach — UNVERIFIED, gating risk.** Codex is Rust;
  whether its client honors `HTTPS_PROXY` + `host.docker.internal` (as the
  claurst fork does) or needs the in-VM relay is **unknown**. **Phase-0 live
  spike decides** whether codex is relay-free (like claurst) or relay-bracketed
  (like occ). The relay is transport-transparent, so either path works; only the
  wiring differs.
- **Capture:** the relay/proxy already forwards `/v1/responses` verbatim, and
  `capture/usage.py` **already** normalizes Responses usage (`input_tokens`/
  `output_tokens`, `response.completed` SSE nesting) and counts `/responses` as
  an inference round. So round + token telemetry mostly works out of the box.

---

## 4. "Full Responses parity" — the wire-shape work

Token/round counting already spans dialects (`capture/usage.py`). The remaining
Chat-Completions assumption is **message-history / role shape**:

1. **Issue #97 assertion (the headline fix).** Add a history-well-formedness
   assertion over captured request bodies in `tests/slow/gates_fixtures.py`
   (that fixture already records every request body via `ScriptedBackend.
   capture_file`). Assert: for a harness driven through a multi-turn tool loop,
   each tool-result in the resent history is preceded by an assistant turn
   bearing the matching call id (assistant-turn count non-zero, ids resolve). The
   extractor is **wire-shape-aware**, keyed on `get(harness).wire_protocol`:
   - `CHAT` → `messages[]` with `role` + `tool_calls[].id` ↔ `tool_call_id`.
   - `RESPONSES` → `input[]` items: `function_call` (`call_id`) ↔
     `function_call_output` (`call_id`), with an assistant `message`/`reasoning`
     item present. (Codex's history shape.)
   This is a **general correctness guard** for the surviving harnesses
   (opencode/claurst/codex). It **would** have caught the occ dropped-turns bug;
   occ's deletion removes the known offender, so the guard is a regression net
   going forward, not a live occ reproducer (see §5 for the stub half).
2. **`render_transcript`** (`telemetry/wire_metrics.py:214`) — add a `RESPONSES`
   branch (`input[]` items → readable turns) so codex transcripts render.
3. Verify token/headroom columns populate against Ollama's experimental
   `/v1/responses` **live** (the parser is ready; the endpoint's emitted `usage`
   is the unknown — parallels the opencode-responses "0 tokens" gap, which this
   same code path also improves).

### The #97 stub half (history-blind stub)

Issue #97 names two causes: (a) gate-only assertions — fixed by §4.1; (b) the
stub AI is **history-blind** (`stubai/script.py` `ScriptEngine.next_reply()`
never reads inbound `messages`, so a malformed history can't change its reply).
Because occ (the only harness that produced malformed history) is being deleted,
a history-*aware* stub is **not required to fix #97** — the well-formedness
assertion over real captured bodies is the durable guard, and it holds for the
surviving harnesses without a smarter stub. We therefore **fix #97 via §4.1** and
record the stub-history-awareness idea as an explicit deferred follow-up in the
issue, rather than build a reproducer for a harness that no longer ships.
(If desired, a small `RESPONSES`/`CHAT`-aware history echo in the stub is a
cheap add — flagged, not scheduled.)

---

## 5. Execution — staged PR stack

### Phase 0 — Codex live spike (branch `spike-codex-harness`, throwaway/`scratch/`)

Answers the unknowns before design lock (mirrors the occ/claurst M0 spikes).
**In the Docker sandbox only** (never host). Confirm, live:
1. `codex exec --json` against **host Ollama ≥ 0.13.3** `/v1/responses` from
   inside the sandbox — does it reach it **relay-free** (proxy-honored) or need
   the relay bracket? Pin the exact working argv + `config.toml`.
2. The `CodexTurn` event schema (`--json` NDJSON: `item.*` / `turn.completed`
   fields for assistant text, tool calls, tokens, stop reason).
3. Whether codex advertises a **bash-like tool** the gate-loop fixture can call
   (today `LOOP_TOOL="bash"`; codex may use `shell`/`exec_command`).
4. Does `usage` populate on `turn.completed` against Ollama's Responses endpoint?
Output: findings appended to this DoR + `.docs/codex-integration.md`. **No spike
code is promoted without full standards (constitution "Scratch" escape hatch).**

### Phase 1 — API + registry + convert opencode/claurst/claude (behavior-preserving)

Base: **stack on `bench-claude-inert-model-skip`** (unmerged; already edits
`bench.py`, which Phase 1 rewrites — stacking avoids re-introducing conflicts per
the Branch policy). If the user merges that PR first, the stack still applies
cleanly. Branch `harness-api-registry`.

1. Create `danno_validator/harnesses/` — `Harness`/`WireProtocol`/`HarnessKind`,
   `register`/`get`/`all_names`.
2. Move each existing harness's unique code into its module
   (`opencode.py` — **new**; `claurst.py`, `claude.py` ← from `baseline.py`),
   incl. its `*Turn` parser (from `driver.py`) and the functions bound into the
   `Harness`. opencode finally gets a real module (it was the implicit "default"
   branch).
3. Replace every §1 dispatch site with a registry lookup. Keep `driver.py` as the
   shared seam only.
4. Self-discovering config schema (§2.4).
5. Behavior-preserving: **no functional change**; existing fast tests
   (`test_validator_driver*`, `test_sandbox`, `test_validator_bench`, …) must
   pass with only import/name updates. `ninja check` green.

### Phase 2 — drop occ (branch stacked on Phase 1)

Delete: `danno_validator/occ.py`; `OccTurn`/`occ_run`/`occ_model_target`/`OCC_*`
in `driver.py`; occ branches in `sandbox.py`, `bench.py`, `run.py`,
`telemetry/provenance.py`; occ from `harnesses/__init__` imports and reap
patterns; occ tests (`test_validator_occ.py`, `test_validator_driver_occ.py`) and
occ refs in `test_sandbox.py`/`test_validator_bench.py`/`test_cli.py`/`conftest.py`/
`tests/data/danno.toml.maximal.example`; occ from the slow `HARNESSES` lists and
docs (`README.md`, examples). Preserve the **dropped-turns finding** in
`.docs/` + memory (already captured). `ninja check` green.

### Phase 3 — add codex + full Responses parity + #97 fix (branch stacked on Phase 2)

1. `harnesses/codex.py` — `CodexTurn`, `generate_codex_config`, install, launch,
   turn_fn, cloud/model resolution, `wire_protocol=RESPONSES`,
   `supports_capture=True` (per Phase-0). `register(...)`.
2. Doctor + bench pre-flight: **Ollama ≥ 0.13.3** for codex (fail loud below it).
3. §4.1 wire-shape-aware #97 assertion in `gates_fixtures.py`; slow `HARNESSES`
   → `["opencode", "claurst", "codex"]`; `LOOP_TOOL` per Phase-0.
4. §4.2 `render_transcript` RESPONSES branch.
5. Docs: `README.md` harness table, `benchmarks.toml`/`danno.toml` examples,
   `--help`/CLI strings, `docs/` harness-API how-to ("how to add a harness"), and
   this DoR's final state. `.docs/codex-integration.md`.
6. **Re-run fast (`ninja check`) AND slow (`uv run pytest -m slow`)** — the slow
   suite exercises the new #97 assertion + codex live. Record results.

---

## 6. Risks / gotchas

- **Codex proxy reach (Phase-0 blocker).** If Rust codex ignores `HTTPS_PROXY`,
  it needs the relay bracket; the design supports both, but the spike must decide
  before codex config-gen is written.
- **Ollama ≥ 0.13.3 hard dependency** for codex-local; non-stateful
  `/v1/responses` may change multi-turn behavior vs api.openai.com.
- **book_em_danno → danno_validator import direction.** The registry lives in
  danno_validator; sandbox.py/schema.py already local-import into it — keep that,
  never invert it (would cycle).
- **Behavior-preserving Phase 1 is the trap.** Moving four harnesses' code at
  once risks silent drift in the stream-json parsers. Move one harness fully,
  gate green, then the next — do not batch.
- **No blind rename.** The `agent`/`harness` split is settled
  (`.docs/rename-agent-to-harness.md`); this work touches the **outer** harness
  layer only — do not disturb the inner `[agents]` persona layer or
  `config/generate.py`'s agent-block generation.
- **claude stays partial.** Do not force claude to implement dialer methods that
  are meaningless; `kind=REFERENCE` + capability flags gate that at call sites.

---

## 7. Done criteria

- Every harness's unique code is in exactly one `harnesses/<name>.py`; grep for
  `if harness ==` / `== CLAURST` / `== OCC` / `== "claude"` returns only the
  registry internals.
- Adding a harness needs **no** edit outside `harnesses/` except the one
  `__init__` import line (and docs).
- occ is fully gone; `all_names() == ("opencode", "claurst", "claude", "codex")`.
- `danno bench --harness codex` runs the aider matrix against local Ollama with
  populated round/token/headroom columns; runaway gates fire.
- Issue #97: the well-formedness assertion is green for opencode/claurst/codex in
  the slow suite.
- `ninja check` green; `uv run pytest -m slow` run and results recorded.
