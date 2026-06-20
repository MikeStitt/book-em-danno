# Agents & cloud-backend refactor — research, findings, proposal

> **Living document.** Research + design captured June 2026 while debugging the
> `temp/example-project` install. Started as "why doesn't the `cloud` backend
> reach `opencode.jsonc`?" and grew into how danno should integrate with
> OpenCode's agent-definition system (JSON + markdown). `.docs/` is **exempt from
> markdown/format quality checks** — this is a knowledge dump, not a spec.
> Empirical claims were verified against opencode (the build inside the
> `docker sandbox` prebuilt `opencode` agent, plannotator v0.21.0) on
> 2026-06-20; **re-verify before relying** — opencode moves fast.
> Companions: [`connecting-claude-code-to-models-investigation.md`](connecting-claude-code-to-models-investigation.md),
> [`user-experience-elephant.md`](user-experience-elephant.md).

## TL;DR

- **The `cloud` backend kind is effectively dead weight.** Its `provider` field is
  read by no code path; for a cloud model the backend only signals "emit
  `model.id` verbatim." It emits **no provider block**, and OpenCode already knows
  cloud models (e.g. `anthropic/claude-sonnet-4-6`) via its built-in catalog +
  launch-time auth — so emitting one would be redundant. **Plan: retire `cloud`.**
- **Reframe `[agents]` to accept a raw OpenCode ref inline.** An agent value with a
  `/` is a passthrough ref (`anthropic/claude-sonnet-4-6`); without a `/` it's a
  `[models]` name. This removes the `[backends.*]` + `[models.*]` ceremony for
  built-in cloud models entirely. (**Refactor A**.)
- **OpenCode merges JSON `agent.<name>` with markdown `.opencode/agent[s]/<name>.md`
  field-wise — but markdown WINS every conflict, including `model`.** Verified.
- **No ADOS agent pins `model:` in frontmatter** (verified across all 22). So
  danno's model-routing overlay via the generated JSON is **authoritative for every
  ADOS agent** and is the right integration seam.
- **Design contract:** danno owns the JSON layer (model assignment + glue);
  behavior (prompt/tools/mode) stays in markdown danno never writes; `model` is
  danno's guaranteed lever; **fail loud** when a danno.toml field would be shadowed
  by a markdown frontmatter field.

---

## 0. Motivation

`[agents]` today is `dict[str, str]` → emitted as a single field per agent:

```python
# src/book_em_danno/config/generate.py
agent_block = { agent: {"model": model_ref(config, name)}
                for agent, name in config.agents.items() }
```

So danno can only say *which model* an agent uses. None of OpenCode's agent
richness — `mode`, `prompt`, `tools`/`permission`, `temperature`, `description` —
is expressible, and there was no articulated story for (a) routing OpenCode's
built-in subagents to local models, or (b) coexisting with the markdown agent
defs ADOS already drops project-local. This is the PoC-stage thinness that
prompted the refactor.

---

## 1. OpenCode agent configuration (reference)

Source: <https://opencode.ai/docs/agents/> (fetched 2026-06-20) + empirical
confirmation in the sandbox.

Two definition methods feed **one** agent registry:

### JSON — `.opencode/opencode.json[c]` (project) or global, under `"agent"`
```json
"agent": {
  "review": {
    "mode": "primary|subagent|all",
    "description": "...",                 // required
    "model": "provider/model-id",
    "prompt": "{file:./prompts/review.md}",
    "temperature": 0.1, "top_p": 0.9, "steps": 5,
    "disable": false, "hidden": false, "color": "#FF5733",
    "permission": { "edit": "allow|ask|deny",
                    "bash": { "*": "ask", "git status *": "allow" }, ... }
  }
}
```

### Markdown — one file per agent
- Project: `.opencode/agents/<name>.md` (docs) — **and** `.opencode/agent/<name>.md`
  (singular) also works; ADOS uses singular. **Both are accepted.**
- Global: `~/.config/opencode/agents/<name>.md`.
- Filename = agent name. YAML frontmatter carries the **same fields as JSON except
  `prompt`**; the **markdown body IS the system prompt**.

### Other load-bearing facts
- **`mode`**: `primary` (Tab-selectable; built-ins `build`, `plan`) ·
  `subagent` (invoked by primaries / `@mention`; built-ins `general`, `explore`,
  `scout` — names vary by version, see probe) · `all`.
- **Override a built-in** by defining its name in either form.
- **Scope precedence**: project beats global for the same name.
- **Introspection** (used throughout this doc):
  `opencode agent list`, `opencode debug agent <name>` (resolved JSON for one
  agent), `opencode debug config` (full resolved config).

---

## 2. Empirical findings (sandbox probes)

All run via `docker sandbox exec danno-temp-example-project bash -lc '…'`
against `/Users/mikestitt/projects/temp/example-project`. Exact commands in the
appendix.

### 2.1 The `cloud` backend is inert
- `grep` of `src/` shows **`CloudBackend.provider` is referenced by no code path.**
  `model_ref` (generate.py:65) returns `model.id` and ignores the backend; the
  catalog loop (generate.py:81-116) emits provider blocks only for
  `OllamaBackend | OpenAIBackend`. A cloud backend therefore emits **nothing**,
  and an *unassigned* cloud model (e.g. the example's `sonnet`) reaches
  `opencode.jsonc` **nowhere**.

### 2.2 OpenCode surfaces cloud models via auth, not via our config
- `opencode models` against the **current no-cloud-block** config listed **306**
  models, including the full native `anthropic/*` catalog — notably
  `anthropic/claude-sonnet-4-6` — with **no `anthropic` block anywhere in our
  `opencode.jsonc`**. The command lists OpenCode's entire known catalog
  (models.dev: anthropic, amazon-bedrock, google, groq, nvidia, opencode, …),
  independent of our config.
- Consequence: emitting `provider.anthropic.models["claude-sonnet-4-6"] = {}`
  (the once-considered "make cloud like ollama/openai" change) duplicates a model
  OpenCode already has and already surfaces once authed → **redundant**.
- Caveat preserved for honesty: `opencode models` proves OpenCode *knows* the
  model, not strictly that the TUI *picker* filters it in; but built-in catalog
  model + authenticated provider (we inject `ANTHROPIC_API_KEY` /
  `CLAUDE_CODE_OAUTH_TOKEN` at launch) ⇒ picker shows it. Our config is not in
  that chain.
- Naming note: for a `cloud`/built-in provider the picker shows the **canonical**
  id (`anthropic/claude-sonnet-4-6`), never danno's backend name
  (`danno-anthropic`) or model name (`sonnet-danno`). For `ollama`/`openai` the
  danno **backend name** does appear (it's the provider key), but the danno
  **model name** never does (the `tag`/`id` does).

### 2.3 JSON × markdown merge precedence — **the pivotal finding**
Defined an agent `probe` in **both** forms at project scope and read back
`opencode debug agent probe`:

| Field | JSON value | Markdown value | **Resolved** | Winner |
|---|---|---|---|---|
| `model` | `qwen3-coder-next` | `gpt-oss:20b` | `gpt-oss:20b` | **markdown** |
| `temperature` | `0.99` | `0.33` | `0.33` | **markdown** |
| `mode` | *(unset)* | `subagent` | `subagent` | markdown (sole setter) |
| `description` | *(unset)* | marker | marker | markdown (sole setter) |
| `prompt` | *(unset)* | body text | body text | markdown (sole setter) |
| `permission.bash` | *(unset)* | `deny` | `deny` | markdown (sole setter) |

- **Merge is field-wise** (disjoint fields combine into one agent), **but markdown
  beats JSON on any conflict — including `model`.** So at the same (project)
  scope, `.opencode/agent[s]/*.md` has higher precedence than our generated
  `opencode.jsonc`.

### 2.4 ADOS agent frontmatter audit
- 22 agent defs in `agentic-delivery-os/.opencode/agent/` (singular dir).
- **None pin `model:` in frontmatter** (verified with a frontmatter-only extract;
  a stray `^model:` grep hit was body prose in `toolsmith.md`).
- Frontmatter fields actually used: `description`/`mode`/`source` (≈all);
  `tools` (external-researcher, image-generator, pm, pr-manager,
  review-feedback-applier, reviewer, toolsmith); `temperature` /
  `reasoningEffort` / `textVerbosity` (review-feedback-applier, reviewer,
  toolsmith).

---

## 3. Design conclusions / the contract

1. **danno owns the JSON layer** (`opencode.jsonc` `agent` block), generated from
   danno.toml.
2. **Behavior (prompt/tools/mode) stays in markdown** — ADOS's or the user's.
   danno **never writes or edits `.opencode/agent[s]/*.md`** (keeps our
   non-destructive/idempotent guarantee and the ADOS-provenance rule).
3. **`model` is danno's guaranteed lever** for every markdown-defined agent,
   because none of them pin it. This is the right integration seam and matches the
   constitution's ADOS contract ("configure model assignment… do not fork agent
   behavior").
4. **danno-defined agents with no markdown** can be expressed *fully* in JSON
   (model + mode + temperature + permission + `prompt = "{file:…}"`). This is the
   "smooth local agents" path — nothing shadows them.
5. **Fail loud on collision** (Working Rule 8): at generate time, scan
   `.opencode/agent[s]/*.md` frontmatter; if danno.toml sets a field the markdown
   also sets (most importantly `model`, but also the 3 tuning agents'
   `temperature`/`reasoningEffort`), **warn that markdown will win** rather than
   silently emitting a shadowed value.

---

## 4. Proposal

### Refactor A — inline refs in `[agents]`, retire `cloud`
Reframe an `[agents]` value to be **either** a `[models]` name **or** a raw
OpenCode ref, disambiguated by `/` (danno model/agent *names* never contain `/`;
OpenCode refs always do):

```toml
[agents]
plan  = "qwen3-coder-next"             # bare name → resolved via [models] (needs a provider block)
build = "qwen3-coder-next"
pm    = "anthropic/claude-sonnet-4-6"  # contains "/" → raw OpenCode ref, passed through verbatim
```

This deletes the entire `[backends.danno-anthropic]` + `[models.sonnet]` ceremony
for built-in cloud models and lets you pick the exact provider+version inline
(`anthropic/…` vs an `amazon-bedrock/…` sonnet).

Then **remove `cloud` / `CloudBackend`** (now redundant; `provider` was already
dead config).

**Code surface**
- `schema.py` `_check_references`: skip the `value in models` check when the value
  contains `/`; add a guard that model/agent **names** contain no `/` (fail loud at
  the boundary). Drop `CloudBackend` from the `Backend` union.
- `generate.py`: small resolver — `"/" in value → return value` else
  `model_ref(config, value)` — used by `agent_block` + `default_agent`/`main_ref`;
  **guard the `small_ref` loop** to skip raw-ref values (not in `config.models`,
  would `KeyError`). Remove the `CloudBackend` branch from `model_ref`. Catalog
  loop unchanged (raw refs aren't `[models]`, so no provider block — correct).
- `danno.toml.example` + `tests/data/danno.toml.maximal.example`: replace the
  cloud backend/model with an inline ref; update tests that assert the cloud path
  (`test_render_maximal_*`, the `_npm_config`/`CloudBackend` test helpers,
  `test_load_*`).
- README §config + the `parts/` ollama/cloud notes.

### Agent layer — rich `[agents]` + markdown overlay
Extend agents from `dict[str, str]` to `dict[str, str | AgentSpec]`:

```toml
[agents]
plan = "qwen3-coder-next"               # shorthand (string): name or raw ref
pm   = "anthropic/claude-sonnet-4-6"

[agents.explore]                         # route OpenCode's built-in explorer to a local model
model = "qwen3-coder-next"

[agents.architect]                       # rich form: model + safe pass-through fields
model  = "anthropic/claude-sonnet-4-6"
mode   = "subagent"
prompt = "{file:./prompts/architect.md}"
[agents.architect.permission]
edit = "deny"
```

- `AgentSpec.model` resolves by the same `/`-rule; other fields validated then
  emitted verbatim into `agent.<name>`.
- **Two wins this unlocks:** (1) override built-in subagents to run locally
  (`[agents.explore] model = "qwen3-coder-next"`); (2) full JSON definition of new
  danno-owned agents (no markdown needed).
- **Collision warning** at generate time (see contract §5).

### Sequencing
1. Refactor A first (small, self-contained).
2. Then the agent layer (depends on the merge contract proven in §2.3).

---

## 5. Open questions / to-verify-before-relying
- **Picker vs catalog**: §2.2 proves OpenCode *knows* cloud models; a TUI check
  (open the picker) would confirm filtering. Not blocking the proposal.
- **Cross-scope precedence**: we proved markdown > JSON at the **same** (project)
  scope. Global-vs-project interactions (e.g. a global JSON model vs a project md)
  were not exercised; danno operates project-local, so this is low-risk but
  unverified.
- **`tools` field shape** in JSON vs markdown (ADOS uses a `tools: { "github*":
  true }` map) — confirm danno passthrough emits the shape OpenCode expects if we
  ever let danno.toml set `tools` (the contract currently keeps `tools` in
  markdown).

---

## Appendix — reproducible probe commands

Sandbox already provisioned as `danno-temp-example-project` (see the install log
in chat history). All non-TTY so they run headless:

```bash
# 2.2 — does the catalog include anthropic with no cloud block?
docker sandbox exec -w /Users/mikestitt/projects/temp/example-project \
  danno-temp-example-project \
  bash -lc 'ANTHROPIC_API_KEY=dummy NVIDIA_API_KEY=dummy opencode models' \
  | grep -iE 'anthropic|sonnet'

# 2.3 — merge precedence: define `probe` in BOTH forms, then:
docker sandbox exec danno-temp-example-project bash -lc '
  cd /Users/mikestitt/projects/temp/example-project
  opencode debug agent probe | jq "{model, temperature, description, prompt}"'
#   .opencode/agents/probe.md frontmatter: mode/temperature/model/permission + body
#   opencode.jsonc agent.probe: { "model": …, "temperature": 0.99 }

# 2.4 — do ADOS agents pin a model in frontmatter? (frontmatter-only extract)
cd /Users/mikestitt/projects/agentic-delivery-os/.opencode/agent
for f in *.md; do
  awk 'NR==1&&/^---$/{i=1;next} i&&/^---$/{exit} i' "$f" | grep -qE '^model:' && echo "$f pins model"
done   # → (no output): none pin model

# introspection surface
docker sandbox exec danno-temp-example-project bash -lc 'opencode debug --help; opencode agent list'
```
