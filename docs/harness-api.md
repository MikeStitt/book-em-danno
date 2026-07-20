# How to add a harness

A **harness** is the outer coding tool danno drives inside a Docker sandbox
(opencode, claurst, claude, codex). Each harness is **one value object** —
`danno_validator.harnesses.Harness` — registered in a self-discovering registry.
The rest of the system (install, config-gen, env, model-resolution, cloud-auth,
capture, launch, telemetry, matrix membership) reads the registry instead of
branching on the harness name. There are no `if harness == …` chains to touch.

**Adding a harness = a new module in `src/danno_validator/harnesses/` + one import
line at the bottom of `harnesses/__init__.py` (and docs).** Nothing else outside
`harnesses/` should need to change. This is the acceptance bar from the design of
record, `.docs/plan-harness-api.md`. codex (added in Phase 3) is the worked
example below.

## 1. Pick a kind and a wire protocol

Two `HarnessKind`s share one contract (`harnesses/__init__.py`):

- **`DIALER`** — danno points it at an endpoint it controls (local Ollama or an
  OpenAI-compatible cloud): opencode, claurst, codex. Dialers share the model
  matrix, cloud-auth, dial-ref, and `--capture` machinery.
- **`REFERENCE`** — carries its own endpoint + auth and selects by native
  `--model` over inert-backend models: claude. It fills the dialer-only seams
  with meaningful no-ops (`dial_ref` → None, `cloud_env_lines` → []), guarded at
  call sites by `kind` / `supports_capture`.

`WireProtocol` routes capture, wire metrics, and the issue-#97 history
well-formedness assertion: `CHAT` (OpenAI chat-completions — opencode, claurst),
`RESPONSES` (OpenAI Responses API — codex), `ANTHROPIC` (claude). A new wire
shape needs a branch in `telemetry/wire_metrics.py::_render_message` and the #97
assertion in `tests/slow/gates_fixtures.py`; an existing one is free.

## 2. Write the implementation seams

Put the harness-specific code in `src/danno_validator/<name>.py` (install, the
`TurnFn` factory, interactive-launch script) and — if it drives a new turn shape
— a `<name>_run` + `<Name>Turn` in `driver.py` that parses the CLI's event stream
onto the shared `Turn` read surface (`ok`, `assistant_text`, `tool_calls`,
`tokens`, `session_id`, `errors`, …). Provisioning/config lives in
`book_em_danno` (`config/generate.py`, `commands/sandbox.py`), reached by the
registry via **function-body local imports only** — never invert the
`danno_validator → book_em_danno` dependency (it would cycle).

codex's seams: `danno_validator/codex.py` (`install_codex`,
`interactive_launch_script`, `authed_codex_run`), `driver.py::codex_run` +
`CodexTurn`, and `book_em_danno/config/generate.py::codex_config_toml` /
`commands/sandbox.py::resolve_codex_model`.

## 3. Bind it into the registry

`harnesses/<name>.py` imports the seams and calls `register(Harness(...))`. Fill
every required field; leave the optional (defaulted) fields unset when the
capability is absent — the call site handles the absence (fail loud / no-op). See
`harnesses/codex.py` for a minimal dialer: `kind=DIALER`,
`wire_protocol=RESPONSES`, `sandbox_image="shell"` (installed post-provision),
`supports_capture=True` + `capture_via_relay=True` (relay-free, like claurst),
`overrides_key=None` (no danno-generated config to override), and the
`install` / `env_lines` / `launch_argv` / `turn_fn` / `dial_ref` /
`model_matrix` / `provenance` / `resolve_start` seams.

Then add the import line at the bottom of `harnesses/__init__.py`. **Import order
is registration order, which sets the bench report's column layout — append, keep
existing order stable.**

## 4. Pre-flight and doctor (if the harness has a hard dependency)

A harness with a host-side prerequisite should **fail loud up front**, not error
mid-sweep (Working Rule 8). codex speaks only the Responses API, which Ollama
exposes from **0.13.3**; so `commands/ollama.py::responses_api_ready` gates it,
`suites/bench.py::run_bench` raises `CommandFailedError` before the sweep when the
probe returns `False`, and `commands/doctor.py` WARNs (a codex-only dependency, so
not a hard doctor failure).

## 5. Tests

- Unit (fast, no Docker): command construction + turn parsing with subprocess
  stubbed (`tests/test_validator_driver_<name>.py`,
  `tests/test_validator_<name>.py`), config-gen (`tests/test_generate.py`),
  model resolution (`tests/test_sandbox.py`), and any pre-flight/doctor gate.
- Slow (live Ollama/docker): add the name to `HARNESSES` in
  `tests/slow/test_gates_termination_matrix.py`; the runaway-gate matrix +
  the #97 well-formedness assertion then cover it. A harness with no polite-stop
  cap stays **out** of `GRACEFUL_HARNESSES` (it relies on the external Gate 1,
  like opencode and codex).

## 6. Docs

Update this file's example if the contract changed, the README harness list +
`--harness` sections, the `--help` strings in `src/book_em_danno/cli.py`, the
`danno.toml` / `benchmarks.toml` examples if the harness is configurable, and the
DoR (`.docs/plan-harness-api.md`). Run `ninja check` (fast gate) and
`uv run pytest -m slow` (live) and record the results.
