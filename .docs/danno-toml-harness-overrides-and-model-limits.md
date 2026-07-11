# danno.toml harness overrides + model-level limits

**Status:** design of record (DoR), pre-implementation.
**Branch:** `danno-toml-harness-overrides` (off `main`).
**Scope:** two related changes to how `danno.toml` drives generated harness config.

---

## 1. Motivation

`danno` generates each harness's config from a *closed, hardcoded* vocabulary. When
opencode or a model needs a knob danno doesn't model, there is **no path** to set it
from `danno.toml` — the only workaround is hand-editing the generated file *inside the
`danno:managed` markers*, which the next `danno config generate` silently overwrites.

Concrete driving case (observed in `../temp/opencode-openai/.opencode/opencode.jsonc`,
a hand-edited target): OpenAI's o-series (`o4-mini`) needs **two** things danno.toml
cannot express:

1. the **native** `@ai-sdk/openai` provider package (danno hardcodes
   `@ai-sdk/openai-compatible` at `generate.py:325`), and
2. a per-model **`max_completion_tokens`** option (`Model` is `extra="forbid"`; only
   `reasoningEffort` is emitted).

Both edits sat inside the managed markers → the next generate would wipe them. This DoR
makes such customization **declarative, durable, idempotent, and diffable**.

A second, independent ask: **`limit.context` / `limit.output` should be specifiable per
model**, not per backend (their current home).

---

## 2. Two features

### Feature A — co-located per-harness overrides

A general escape hatch: `[<element>.overrides.<harness>]` sub-tables, **co-located with
the danno.toml element they modify**, deep-merged into that element's generated block for
that harness.

**Harnesses in scope:** `opencode`, `claurst`. (`occ` is **out** — see §6. `claude` has no
generated config.)

The danno.toml → generated-config correspondence that makes co-location work:

| danno.toml element   | opencode.jsonc region                     | claurst region                       |
| -------------------- | ----------------------------------------- | ------------------------------------ |
| `[backends.<n>]`     | `provider.<n>`                            | `models.json` provider entry         |
| `[models.<n>]`       | `provider.<backend>.models.<tag>`         | `models.json` model entry            |
| `[agents.<n>]`       | `agent.<n>`                               | `settings.json` agent                |
| `[defaults]`         | top-level `model`/`small_model`/`default_agent` | —                              |

**Examples** (the driving o4-mini case):

```toml
[backends.danno-openai]
kind        = "openai"
base_url    = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[backends.danno-openai.overrides.opencode]      # deep-merged into provider.danno-openai
npm = "@ai-sdk/openai"                           # beats hardcoded @ai-sdk/openai-compatible

[models.o4-mini]
backend          = "danno-openai"
tag              = "o4-mini"
reasoning_effort = "high"
context_budget   = 200000                        # Feature B (see below)
output_limit     = 65536

[models.o4-mini.overrides.opencode.options]      # merged INTO the generated options
max_completion_tokens = 1000000                  # reasoningEffort stays; this is added
```

**Semantics:**

- **Deep-merge, override wins.** Objects merge recursively; scalars and **arrays replace
  wholesale** (arrays don't key-merge — documented, not clever).
- **Applied at generation time**, after danno builds its doc dict, before serialization,
  **inside** the `danno:managed` markers. So re-run is idempotent and the diff shows it.
- **Closed harness sub-keys.** `overrides`'s keys are validated to `{opencode, claurst}`
  → a typo (`[models.x.overrides.opencdo]`) **fails loud**. Everything *below* the harness
  key is intentionally open (that's the escape hatch) — typed as `dict[str, Any]`.
- **Honest diff.** Each node an override replaces/adds gets a `// danno-override:`
  annotation in the emitted managed region, so the diff never hides that danno's own value
  was superseded (Working Rule 8, fail-loud transparency).
- **Same two-tier flow.** Rides the existing advise-by-default / `--apply` diff path — so
  it is reversible (remove from danno.toml → next generate reverts) and reviewable.

**Asymmetries made visible by co-location (stated, not hidden):**

- **Structural opencode keys with no danno element** — top-level `$schema`, the `plugin`
  array. Top-level `model`/`small_model`/`default_agent` are reachable via
  `[defaults.overrides.opencode]`. `plugin` is already `[[npm]]`; overriding the array
  replaces wholesale.
- **claurst provider keying** — claurst's registry is keyed by claurst provider id
  (`ollama`/`nvidia`), derived from the backend. `[backends.<n>.overrides.claurst]` targets
  that derived entry.

**Security guardrail (per `sandbox-security-contract-fail-loud`):** overrides ride a
visible, diff-gated flow, but they *can* reach egress-sensitive nodes (`options.baseURL`,
`apiKey`). An override that changes a baseURL / apiKey / proxy-adjacent node emits a
**loud warning** (not a silent apply) so weakening the sandbox contract can never look like
a routine override. It is still the developer's own committed config, so we warn rather
than forbid.

### Feature B — model-level `context_budget` / `output_limit`

Move both fields from the **backend** to the **model**, as typed first-class fields (NOT
via the Feature-A hatch — limits are a real cross-harness concept both opencode and claurst
emit).

- **Hard-move:** remove `context_budget` / `output_limit` from `OllamaBackend` and
  `OpenAIBackend`. Add them to `Model`.
- **Fail loud, scoped to limit-emitting backends:**
  - a model on an `ollama` / `openai` backend that **omits** either field → config error
    at load (`"[models.<n>] needs context_budget and output_limit"`). **No default.**
  - a model on an `inert` / `llamacpp` backend that **sets** either field → config error
    (`"limits are meaningless on backend kind <k>"`). Limits are required exactly where
    used, forbidden where not.
- **One shared resolver.** Introduce `resolve_limits(config, model) -> (context, output)`
  consumed by **both** emit sites — `_danno_doc` (`generate.py:337`, opencode) and
  `generate_claurst_models` (`generate.py:189`, claurst) — killing the currently-duplicated
  backend read. (This is the one small shared abstraction justified by two real call sites,
  not premature.)

---

## 3. Schema changes (`src/book_em_danno/config/schema.py`)

- **`Model`** (`:107`): add `context_budget: int | None = None`, `output_limit: int | None
  = None`, and per-harness override dicts. Keep `extra="forbid"` — the override *container*
  is a typed field, not a relaxed-extras escape.
- **`OllamaBackend` / `OpenAIBackend`**: remove `context_budget` / `output_limit`.
- **Override container.** Add a small typed model, e.g.
  `Overrides(opencode: dict[str, Any] | None, claurst: dict[str, Any] | None)` with
  `extra="forbid"`, attached as `overrides: Overrides | None = None` on `Model`,
  `OllamaBackend`, `OpenAIBackend`, `AgentSpec`, and `Defaults`.
- **`DannoConfig._check_references`** (`:224`): add the Feature-B limit validation (required
  where emitted, forbidden where not) and any override-target sanity (e.g. reject a claurst
  override on an inert model).

## 4. Generation changes (`src/book_em_danno/config/generate.py`)

- `resolve_limits(config, model)` helper (new).
- `deep_merge(base, override)` helper (new) — objects merge, scalars/arrays replace.
- `_danno_doc`: after building each element's dict, `deep_merge` the element's
  `overrides.opencode` payload; annotate overridden nodes; use `resolve_limits` for the
  `limit` block; emit the security warning on egress-sensitive nodes.
- `generate_claurst_models` / `generate_claurst_agents`: same `deep_merge` of
  `overrides.claurst`; use `resolve_limits`.

## 5. `[env]` — out of scope, but reconciled

`[env]` stays **global** (all config-driven harnesses) for this change — no concrete
per-harness env need with occ out, and simplicity-first (Working Rule 2). It is a **different
surface** from Feature-A overrides — the **launch env-file** (runtime, chmod-600), not the
**generated config file** — so the two are *correctly* disjoint.

Reserved future shape (documented so it stays drop-in, not built now): per-harness env as
`[env.<harness>]` layered over bare `[env]`, precedence `CLI > host > [env.<harness>] >
[env] > default`, resolved in `assemble_harness_env` (already called per-harness). Requires
reserving `{opencode, claurst, occ, claude}` as scope keys (fail loud if used as a literal
env var name).

## 6. `occ` — explicitly out of scope

occ has **no generated config file** — its whole surface is env vars + CLI flags. Env is
already overridable via `[env]`; the only gap is *hardcoded CLI flags* (`--max-turns`,
`--permission-mode` in `driver.py`), which would need a **new flag-override mechanism**, not
a config-tree merge. Deferred. `[<element>.overrides.occ]` is not accepted (fails loud as an
unknown harness key).

## 7. Documentation (same commit — Documentation Hygiene)

- **`tests/data/danno.toml.maximal.example`** — the canonical "every knob" fixture. Add:
  model-level `context_budget`/`output_limit`; a `[backends.*.overrides.opencode]` and
  `[models.*.overrides.opencode.options]` example; and (gap fix) an `[env]` example, which
  is currently undemonstrated there.
- **README.md** — document model-level limits, the `[<element>.overrides.<harness>]` hatch,
  and the `[env]` table (currently README shows only the `--env` *CLI flag*, never the
  table). Note the reserved `[env.<harness>]` future shape.
- Update any `--help` text touched by the schema change.

## 8. Verification (Configuration is Code — exercise it, don't just edit it)

`ninja check` is necessary but **not sufficient** (it doesn't run a real generate). Close
the gap by hand:

1. Feed a `danno.toml` with `[models.o4-mini.overrides.opencode.options] max_completion_tokens`
   + `[backends.*.overrides.opencode] npm = "@ai-sdk/openai"` through `danno config generate`
   and **confirm the emitted opencode.jsonc** carries both, merged (reasoningEffort intact),
   with the `// danno-override:` annotations.
2. Re-run generate → **confirm no-op** (idempotent).
3. Remove the overrides → confirm the values **revert**.
4. Confirm claurst overrides land in `models.json`.
5. **Fail-loud checks:** a limit-omitting ollama/openai model → rejected; a limit-*setting*
   inert model → rejected; an `overrides.occ` / `overrides.typo` key → rejected; a baseURL
   override → loud warning.

## 9. Breaking changes / migration

Feature B is a **breaking config change** (hard-move). Migrate in the same PR:

- `tests/data/danno.toml.maximal.example`, and any other fixture/target danno.toml, move
  limits from `[backends.*]` to `[models.*]`.
- `tests/test_generate.py` (`:134-135, 179, 250-251, 257-258`) construct backends with
  `context_budget=`/`output_limit=` — update to model-level.
- `tests/test_loader.py:181` note.
- Old configs with backend-level limits now **fail loud** (`extra="forbid"`) — intended.

## 10. Decision log

- Overrides **co-located** with their element (`[<element>.overrides.<harness>]`), not a
  central mirror block — more discoverable; sidesteps dotted-path/quoted-tag addressing
  (model tags contain `.`/`:`, e.g. `gpt-oss:20b`). ✅ user
- Harnesses: **opencode + claurst**; **occ out**; claude n/a. ✅ user
- Limits: **model-level, hard-move off backends, fail loud** (required where emitted,
  forbidden where not; no default). ✅ user
- `[env]`: **global for now**, `[env.<harness>]` reserved future shape. ✅ user
- Open: none blocking. (Array-override = wholesale replace; baseURL override = warn not
  forbid — both stated above, revisit if either bites.)
