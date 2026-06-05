# Constitution — ADOS + Ollama integration part

Authoritative knowledge for installing/adjusting Agentic Delivery OS (ADOS) on a
target project and wiring its hybrid local+cloud model runtime. Read this part
when a task touches the install/adjust flow or generates `.opencode/` model
configuration. Read together with the [constitution](../constitution.md) and
[`bash.md`](bash.md).

## What ADOS is and where it lives

- **ADOS** is an MIT-licensed, **OpenCode-based** delivery framework: 19 agents
  (`@pm`, `@architect`, `@spec-writer`, `@coder`, `@reviewer`, …), 16 commands,
  and a 10-phase change lifecycle. Upstream:
  <https://github.com/juliusz-cwiakalski/agentic-delivery-os>. Local checkout:
  `../agentic-delivery-os`.
- **The product is markdown**: `.opencode/agent/*.md` and `.opencode/command/*.md`
  define agent/command behavior. **Behavior is model-agnostic** — model choice is
  set separately in config (see below).
- **Install model** (stock ADOS, `scripts/install.sh`):
  - `--global` → clones to `~/.ados/repo/` and installs agent/command defs to
    `~/.config/opencode/`, making them available in every project. Re-running
    updates in place (idempotent).
  - `--local` → copies framework artefacts into the current project (`doc/`,
    `.ai/`, `.opencode/`), **preserving** project-specific files such as
    `.ai/agent/pm-instructions.md`. Then `/bootstrap` runs guided onboarding.

## This repository's job

Install/adjust ADOS on a target project (e.g. `mesh-atlas`, a `bench-*` run) and
**generate the hybrid local+cloud model configuration that stock ADOS does not
ship.** We do **not** fork ADOS agent/command behavior — per constitution Working
Rule 6 and the _ADOS provenance_ rule, we configure model assignment and provide
the install glue only, and we record which ADOS version a target was installed
from.

## OpenCode model configuration

OpenCode merges configs rather than replacing them. Model assignment lives in
`.opencode/opencode-<provider>.jsonc`, layered over the base `.opencode/opencode.jsonc`:

```
opencode.jsonc (base, MCP + per-agent tool toggles)
  → opencode-<provider>.jsonc (per-agent "model" assignment)  ← we generate this
```

Each agent gets a `{ "model": "<provider>/<model>" }`. ADOS's reference tiering
is in `../agentic-delivery-os/.opencode/opencode-github-copilot.jsonc` — read it
before generating ours, because our hybrid mapping is defined relative to those
tiers.

## Hybrid mapping (the deliverable)

Generate `.opencode/opencode-ollama.jsonc` assigning each agent to **local Ollama
Gemma** or **cloud**, by ADOS tier:

| Tier (ADOS)                | Agents                                                                                         | Runtime                  |
| -------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------ |
| 1–2 — high-stakes / core   | `architect`, `reviewer`, `plan-writer`, `pm`, `doc-syncer`, `toolsmith`, `coder`, `fixer`, `spec-writer`, `test-plan-writer`, `pr-manager` | **Cloud**                |
| 3–5 — well-scoped / cheap  | `committer`, `runner`, `external-researcher`, `image-generator`, `image-reviewer`              | **Local — Ollama/Gemma** |

The split point (which tiers run local) is the project's main tunable. Keep the
table above as the documented default; deviations belong in the generated config
with a comment, not by editing this part.

## Ollama runtime

- Ollama is configured as an **OpenCode provider** exposing an OpenAI-compatible
  local endpoint (default `http://localhost:11434`); local agents reference it as
  `ollama/<model>`.
- **Default local model: Gemma** (the user's target, "gemma 4"). The exact tag
  and quantization is a **tooling-time choice**, picked to fit the developer's
  machine (this is tuned for a local MacBook Pro) — do not hard-pin a tag in this
  constitution part; surface it as a script flag / config default.
- The cloud half uses whatever provider the developer already has configured in
  OpenCode (Anthropic, GitHub Copilot, etc.); the generator only sets the local
  half and leaves cloud assignments overridable.

## Idempotency & preservation (restated for generated config)

- Re-running the generator MUST converge: regenerate `opencode-ollama.jsonc`
  deterministically; do not append duplicate keys or churn unrelated formatting.
- Never overwrite a developer's hand-edited cloud assignments or
  `pm-instructions.md` — detect and preserve, per constitution Working Rule 6.
- Record the source ADOS version (e.g. `~/.ados/repo` git SHA) somewhere
  traceable so a later update knows what it is upgrading from.

## See also

- [`../constitution.md`](../constitution.md) — Working Rule 6 (non-destructive
  installs) and the ADOS-provenance rule.
- [`bash.md`](bash.md) — how the install/generate scripts must be written.
