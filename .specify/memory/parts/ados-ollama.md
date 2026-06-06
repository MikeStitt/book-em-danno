# Constitution — ADOS + Ollama integration part

Authoritative knowledge for installing/adjusting Agentic Delivery OS (ADOS) on a
target project and wiring its hybrid local+cloud model runtime. Read this part
when a task touches the install/adjust flow or generates `.opencode/` model
configuration. Read together with the [constitution](../constitution.md) and
[`bash.md`](bash.md).

## What ADOS is and where it lives

- **ADOS** is an MIT-licensed, **OpenCode-based** delivery framework: ~20 agents
  (`@pm`, `@architect`, `@spec-writer`, `@coder`, `@reviewer`, …), 16 commands,
  and a 10-phase change lifecycle. Upstream:
  <https://github.com/juliusz-cwiakalski/agentic-delivery-os>. Local checkout:
  `../agentic-delivery-os`.
- **The product is markdown**: `.opencode/agent/*.md` and `.opencode/command/*.md`
  define agent/command behavior. **Behavior is model-agnostic** — model choice is
  set separately in config (see below).
- **Install model** (stock ADOS, `scripts/install.sh`):
  - `--global` → clones to `~/.ados/repo/` and installs agent/command defs to
    `~/.config/opencode/`. Re-running updates in place (idempotent).
  - `--local` → copies framework artefacts into the current project (`doc/`,
    `.ai/`, templates), **preserving** project-specific files such as
    `.ai/agent/pm-instructions.md`. **It does NOT copy the agent/command defs** —
    those go only to the global dir.

## This repository's job

Install/adjust ADOS on a target project (e.g. `mesh-atlas`, a `bench-*` run) and
**generate the hybrid local+cloud model configuration that stock ADOS does not
ship.** We do **not** fork ADOS agent/command behavior — per constitution Working
Rule 6 and the _ADOS provenance_ rule, we configure model assignment and provide
the install glue only, and we record which ADOS version a target was installed
from.

## OpenCode config activation (the gotcha)

OpenCode **merges** configs, but it only **auto-loads** `.opencode/opencode.jsonc`
(and the project-root `opencode.jsonc`) — it does **not** auto-load
`opencode-<provider>.jsonc` variants (those need the `OPENCODE_CONFIG` env var to
select them). Therefore **we own the project's auto-loaded `.opencode/opencode.jsonc`
wholesale** — stock ADOS installs none, so there is nothing to merge with. Each
agent gets a `{ "model": "<provider>/<model>" }`. ADOS's reference tiering is in
`../agentic-delivery-os/.opencode/opencode-github-copilot.jsonc`.

Because a Docker sandbox **cannot see the host `~/.config/opencode`**, both the
model config (`.opencode/opencode.jsonc`) **and** the agent/command defs
(`.opencode/agent/*.md`, `.opencode/command/*.md`) MUST be installed
**project-local** for the agents to run in the sandbox.

## Hybrid mapping (the deliverable)

Generate the project's auto-loaded **`.opencode/opencode.jsonc`** assigning each
agent to **local Ollama Gemma** or **cloud**, by ADOS tier:

| Tier (ADOS)                | Agents                                                                                         | Runtime                  |
| -------------------------- | ---------------------------------------------------------------------------------------------- | ------------------------ |
| 1–2 — high-stakes / core   | `architect`, `bootstrapper`, `reviewer`, `review-feedback-applier`, `pm`, `coder`, `fixer`, `plan-writer`, `spec-writer`, `test-plan-writer`, `toolsmith`, `designer`, `doc-syncer`, `pr-manager`, `editor` | **Cloud**                |
| 3–5 — well-scoped / cheap  | `committer`, `runner`, `external-researcher`, `image-generator`, `image-reviewer`              | **Local — Ollama/Gemma** |

The split is a table at the top of `tools/gen-opencode-config` (one-line retune).

**Tool-calling caveat (empirical):** every ADOS agent uses OpenCode tools, so a
local model that cannot tool-call is unusable for that agent. `gemma3:1b` does
**not** tool-call (verified) — local agents need a tool-capable Gemma (a larger
size, the user's "gemma 4"), or move them back to cloud. Keep `num_ctx` high
(≈32000) or tool calls fail.

## Ollama runtime

- Ollama is an **OpenCode provider** (`@ai-sdk/openai-compatible`); local agents
  reference it as `ollama/<model>`. The generated config carries a dummy
  `apiKey: "ollama"`, `tool_call: true`, and a context `limit` — the
  openai-compatible provider needs them (a missing apiKey makes OpenCode demand a
  real key).
- **Default local model: Gemma**; the exact tag/quant is a tooling-time `--model`
  flag tuned to the machine — never hard-pinned here.
- The cloud half uses whatever provider the developer has configured in OpenCode;
  keys stay `{env:...}` (never written into the committed config).

The **network model** — the `baseURL`, the `OLLAMA_HOST=0.0.0.0` host bind, the
Docker-sandbox egress policy (internet + Ollama allowed; other host services and
LAN denied), and the `host.docker.internal`→`localhost` rewrite — lives **once**
in the [README "Network model" section][readme-net]. Do not duplicate it here.

## Docker sandbox runtime

Agents run in Docker's `docker sandbox` (Linux microVM), configured by
`tools/ados-sandbox`: `docker sandbox create --name <n> opencode <workspace>`.
**There is no per-sandbox CPU/memory flag** — size the VM in Docker Desktop ▸
Resources. Heavy builds run Linux-target inside the VM; GPU inference stays on the
host (Ollama on Metal). The macOS `sandbox-exec` path (`tools/ados-sandbox-macos`)
is a weaker, experimental write-confinement alternative for macOS-native work
only. Network egress + isolation specifics: the
[README "Network model"][readme-net].

[readme-net]: ../../../README.md#network-model-docker-sandbox

## As-built: the toolkit

| Command | Role |
| --- | --- |
| `tools/ados-ollama-doctor` | read-only preflight |
| `scripts/setup-ollama.sh` | ensure Ollama + pull/verify a Gemma model |
| `scripts/install-ados.sh` | ADOS `--local` + project-local agent/command copy + provenance |
| `tools/gen-opencode-config` | write the hybrid `.opencode/opencode.jsonc` |
| `tools/ados-sandbox` | launch OpenCode in the Docker sandbox |
| `tools/ados-sandbox-macos` | experimental Seatbelt write-confinement |
| `tools/ados-ollama` | orchestrator: `doctor\|install\|setup\|configure\|sandbox\|all` |

## Idempotency & preservation

- Re-running the generator MUST converge: it regenerates `.opencode/opencode.jsonc`
  deterministically and **preserves** an existing differing file unless `--force`.
- Never overwrite a developer's hand-edited config or `pm-instructions.md`.
- Record the source ADOS git SHA (`.opencode/ados-provenance.txt`) so a later
  update knows what it is upgrading from.

## See also

- [`../constitution.md`](../constitution.md) — Working Rule 6 (non-destructive
  installs) and the ADOS-provenance rule.
- [`bash.md`](bash.md) — how the install/generate scripts must be written.
