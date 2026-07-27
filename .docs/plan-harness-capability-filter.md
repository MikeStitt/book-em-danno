# Plan — harness/backend capability filtering (the "speakable" matrix)

Status: **Layer 0 IMPLEMENTED** (2026-07-19, branch `harness-api-add-codex`, fast gate green
670). Layers 1–3 designed, not started. Design-of-record for the discussion that produced it.

## Problem

`danno bench` sweeps `harnesses × models × tasks`. Some cells are structurally impossible —
a harness cannot speak the protocol/backend a model needs (e.g. codex is Phase-0-wired for
local Ollama only; claurst 0.1.6 can't do the OpenAI Responses API o-series models require).
Before this work such a cell **failed loud at env-file build, up front, killing the whole
harness column** (including its *speakable* cells) and the cross-harness comparison report.
The only graceful exclusion was inert-backend models dropped from a dialer's matrix — and
even that was *silent* (the model just vanished from the grid).

## The predicate

A cell `(harness, backend, model)` is **speakable** iff:

```
backend.kind ∈ harness.dials
∧ model.requires ⊆ (harness.speaks ∩ backend.offers)
∧ harness has a provider-identity for the backend
```

Pure set algebra over *declared* facts, so adding/removing a capability is a **data edit**,
never a control-flow change.

## Fail-loud vs. graceful N/A (the rule)

Graceful N/A (drop + `log_warn`, or render an N/A grid cell) **only** for a
*declared, statically-provable, inherent* capability boundary reached *implicitly* (full
cross-product). **Fail loud** for:
- an **explicit** `--only`/single-harness request naming an impossible pairing (else a
  fake-green empty sweep),
- a **fixable operator error** (missing key, typo'd/unknown name, toolchain absent,
  endpoint unreachable, Ollama too old) — never filtered, or it hides the bug,
- anything danno **cannot statically prove** (never filter on a guess).

A reasoned N/A *is* loud reporting ("we did not run this, here's why") — the constitution
forbids a *silent* skip or a *false pass*, and this is neither.

## Where each fact is configured (three homes, by who knows it)

| Fact | Home | Notes |
|---|---|---|
| `harness.speaks` / `harness.dials` | **registry (code)** `harnesses/<name>.py` | property of the tool/version; bind next to the version pin. `wire_protocol` exists but is single-valued — widen to a set for opencode (chat+responses via npm). |
| `backend.offers` | **danno.toml `[backends.*]`** new `wire` field | only the author knows OpenAI-vs-NIM (both `kind="openai"`). |
| `model.requires` | **danno.toml `[models.*]`** optional `requires_wire` | for a protocol-restricted model on a dual-protocol endpoint (o4-mini on api.openai.com); default = inherit from backend. |
| provider identity | already homed | opencode `overrides.opencode.npm`, claurst `claurst_provider`, codex `codex_provider` (proposed). |

Version bump ⇒ edit ONE registry module (pin + capability set). User config is untouched.

## Provable NOW (backend.kind only, zero new config)

- dialer × **inert** — already a filter (`_dialer.openai_compat_variants`).
- **reference** (claude) × non-inert — already enforced (`claude.reference_matrix`).
- **codex × non-Ollama** — `codex_provider_id`/`resolve_codex_model`/`codex_cloud_env_lines`
  already prove it (as raises). **Layer 0 converts the *sweep path* to a filter.**

NOT provable without a new declaration: the Chat-vs-Responses split inside `kind="openai"`.

## Staged plan

### Layer 0 — codex sweep: raise → graceful N/A filter  ✅ DONE
- `harnesses/_dialer.py`: new `dialable_variants(config, only, *, allowed_kinds, harness)` —
  generalizes `openai_compat_variants`; drops un-dialable models from an implicit sweep with
  a `log_warn` (no square brackets — rich markup would eat the model names), fails loud on an
  explicit `--only` naming one.
- `harnesses/codex.py`: `_CODEX_DIALABLE_KINDS = {"ollama"}`; `_model_matrix` binds
  `dialable_variants`.
- Tests: `test_codex_matrix_drops_cloud_model_from_implicit_sweep`,
  `test_codex_matrix_explicit_only_cloud_model_fails_loud`,
  `test_opencode_matrix_still_sweeps_cloud_model` (regression).
- Effect: `--harness opencode --harness codex` over `{o4-mini, qwen}` no longer aborts — codex
  sweeps qwen, o4-mini is a loud N/A; opencode sweeps both. The interactive/resolve-time
  raises stay as the fail-loud backstop for an explicit `sandbox start --harness codex -m o4-mini`.

### Layer 1 — declarative capability sets + N/A grid rendering  ✅ DONE
- Registry: added `speaks: frozenset[WireProtocol]`, `dials: frozenset[str]` to `Harness`
  (opencode speaks={chat,responses}/dials={ollama,openai}; claurst speaks={chat}/dials=
  {ollama,openai}; codex speaks={responses}/dials={ollama}; claude speaks={anthropic}/dials=
  {inert}). Every dialer now binds `dialable_variants` with its own `dials` (opencode/claurst
  via a module `_*_DIALS` const, codex via `_CODEX_DIALABLE_KINDS`) — the inert path is
  UNIFIED (`openai_compat_variants` deleted; the inert drop is just the `"inert" ∉ dials` case,
  now LOUD). `bench._openai_compat_variants` seam repointed to `dialable_variants`.
- Reporting: `run_bench_harnesses` `log_warn`s any harness whose whole matrix was N/A (empty
  column) so the merged grid's blank reads as a reasoned N/A, not a silent gap.
- **Deferred (conscious):** per-CELL `— n/a (<reason>)` rendering inside `merge_markdown`/
  `merge_html`. The merge grid is per-HARNESS-column (`grid[(col, task)]`, last-write-wins
  across a harness's models), not per-(harness,model), so a per-cell N/A reason has no clean
  home without reworking the column model — and once Layer 3 makes codex×o4-mini speakable the
  target 40-cell sweep has NO N/A cells anyway. The per-model drop is already named loudly by
  `dialable_variants`; the column-level warn covers the merge. Revisit if/when the merge grows
  per-(harness,model) columns.
- Gate: fast `ninja check` green (670 passed, 31 deselected).

### Layer 2 — the Chat/Responses declarations  ✅ DONE
- `OpenAIBackend.wire: frozenset[Literal["chat","responses"]] = {"chat"}` (NIM/vLLM default =
  Chat-only; declare `wire = ["chat", "responses"]` for api.openai.com) + optional
  `Model.requires_wire`. `schema.backend_wire_offers(backend)` maps kind→offers (ollama=
  {chat,responses}, openai=`.wire`, llamacpp={chat}, inert={anthropic}).
- `_dialer.dialable_variants` now evaluates the FULL predicate per model: `kind ∈ dials ∧
  (speaks ∩ offers) ≠ ∅ ∧ requires_wire ⊆ (speaks ∩ offers)`, with a specific N/A reason per
  model. Each dialer passes its own `speaks`+`dials` (module consts). The `≠ ∅` clause is the
  one the terse DoR predicate left implicit — needed so codex×NIM (openai kind, Chat-only) is
  N/A even after Layer 3 widens codex `dials` to include `openai`.
- Tests: `test_backend_wire_offers_by_kind`, `test_requires_wire_gates_chat_only_harness`
  (opencode keeps a Responses-only Ollama model, claurst drops it — isolates the wire arm from
  the kind arm), `test_requires_wire_explicit_only_chat_only_harness_fails_loud`.
- Gate: fast `ninja check` green (673 passed).

### Layer 3 — cloud codex seams (enable codex × o4-mini for real)  ✅ DONE
- `codex_provider_id`/`resolve_codex_model`/`codex_cloud_env_lines` OpenAI branches are now
  REAL (were fail-loud backstops): provider id `openai-danno` (a distinct custom block, never
  the reserved built-in `openai`), bare `-m` tag, and key injection delegated to the shared
  `cloud_api_key_env_lines` (fails loud only on an unset host key). No new `codex_provider`
  toml field was needed — codex owns its provider block, so danno derives the id.
- `codex_config_toml` grew `provider_id`/`provider_name`/`reasoning_effort` params; a cloud
  row writes the `openai-danno` block with `base_url` + `env_key` (NAME only) + `wire_api =
  "responses"` + `model_reasoning_effort` (the o-series knob, else silently dropped).
- **Driver plumbing (the crux):** codex writes its config.toml INLINE in the VM per turn, so
  a cloud row's base_url/key-env must reach the driver at turn time (unlike opencode's host-
  generated rewritten config). New `driver.CodexProvider(base_url, env_key, reasoning_effort)`,
  resolved by a new `Harness.dial_provider` seam (only codex sets it) from the **capture-
  rewritten** backend — so cloud codex captures through the SAME recording proxy as opencode
  (`plan_capture` rewrites the explicit-base_url cloud backend to `host.docker.internal:<port>/v1`,
  TLS re-originated to the cloud; no fail-loud-on-capture needed). Threaded through
  `run_turn_for` → the codex `TurnFn` factory ONLY when non-None, so the other three factories
  are untouched (a local/non-codex row resolves to None and takes the ordinary path).
- Widened `_CODEX_DIALABLE_KINDS` to `{"ollama", "openai"}`; the Responses-only `speaks` is
  what gates a Chat-only cloud endpoint (NIM/vLLM) out on its own (Layer 2's `≠ ∅` clause).
- Tests: `test_codex_provider_id_cloud_is_custom_not_reserved`, `test_codex_config_toml_cloud_
  provider`, `test_codex_matrix_includes_responses_capable_cloud_model`, `test_codex_dial_
  provider_cloud_and_local`, `test_harness_dial_provider_none_for_non_codex`, plus the three
  old Phase-0 fail-loud tests converted to their real-cloud counterparts (resolve → bare tag,
  cloud env-lines → key injection / unset-fail-loud).
- Gate: fast `ninja check` green (678 passed, 31 deselected).

## Minimizing danno changes on a harness version bump
Capability = a **versioned, declared-or-probed set on the single registry value object**,
consumed through the seams; guarded by a loud up-front assert (cf. `ollama.responses_api_ready`).
A bump that adds/removes a capability = one small edit in one module; a *wrong* declaration
fails loud, not ships. Only a genuinely new **wire shape** (event schema/argv) needs a driver
change — that cost is inherent, isolated to `driver.py` + the harness module.
