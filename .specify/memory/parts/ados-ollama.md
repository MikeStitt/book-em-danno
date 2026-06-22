# Constitution — ADOS + Ollama integration part

Authoritative knowledge for how `danno` wires an OpenCode hybrid local+cloud
model runtime, and for the special case of installing Agentic Delivery OS (ADOS)
— one tool in danno's catalog. Read this part when a task touches the install
flow, the network model, or generates `.opencode/` model configuration. Read
together with the [constitution](../constitution.md) and
[`python.md`](python.md).

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

## danno's job (and where ADOS fits)

`danno` provisions a target project from `danno.toml`: generate the hybrid
local+cloud `.opencode/opencode.jsonc`, pull the local Ollama models, install the
declared tool catalog, and create the Docker sandbox. **ADOS is one tool in that
catalog** — `danno install` runs its `--local` installer and copies its
agent/command defs project-local (the sandbox can't see host
`~/.config/opencode`). We do **not** fork ADOS agent/command behavior — per
constitution Working Rule 6 and the _ADOS provenance_ rule, we configure model
assignment and provide the install glue only, and record which ADOS version a
target was installed from. Model assignment is written into a danno-managed,
marker-delimited region of the agent `.md`'s frontmatter when that `.md` controls
the agent (markdown beats the generated `opencode.jsonc`, so `model` set there would
be shadowed); the body and behavior fields (`prompt`/`tools`/`mode`) are never
touched. The merge is surgical/idempotent/reversible (`config/generate.py`
`generate_md`).

**Two install lanes.** `[[tools]]` is for **imperative** tools that have their own
installer (ADOS is the archetype). OpenCode **npm plugins** (e.g. `opencode-planner`,
`@plannotator/opencode`) belong in the separate **`[[npm]]`** table instead: they are
**declarative** — danno only lists them in the generated `opencode.jsonc` `"plugin"`
array and OpenCode auto-installs them in the sandbox at startup. A plugin's optional
`setup` commands run post-create via `docker sandbox exec <name> bash -lc …`. Do not
put npm plugins in `[[tools]]`, and do not give an imperative tool an `[[npm]]` entry.

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

The split is the `[agents]` table in `danno.toml` (one-line retune per agent).

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
`commands/sandbox.py` (the `danno sandbox` command):
`docker sandbox create --name <n> opencode <workspace>`. **There is no
per-sandbox CPU/memory flag** — size the VM in Docker Desktop ▸ Resources. Heavy
builds run Linux-target inside the VM; GPU inference stays on the host (Ollama on
Metal). **OpenCode is provided by the prebuilt `opencode` sandbox image and only
ever runs in-container** — never on the host (no host `opencode` dependency, no
macOS Seatbelt path). Network egress + isolation specifics: the
[README "Network model"][readme-net].

[readme-net]: ../../../README.md#network-model-docker-sandbox

## As-built: the danno CLI

Three commands; the rest are internals orchestrated by `install`.

| Surface | Role |
| --- | --- |
| `danno install` | orchestrator: validate → config → Ollama models → tools → sandbox-create → npm plugin setup, then prints the launch hint (stops before the TUI) |
| `danno doctor` | read-only preflight (`commands/doctor.py`) |
| `danno sandbox <start\|shell\|stop\|rebuild\|update>` | operate the Docker sandbox (`commands/sandbox.py`) |
| `commands/ollama.py` (internal) | reachability + ensure/verify models + tool-call probe |
| `commands/tools.py` (internal) | install the tool catalog; ADOS `--local` + project-local agent/command copy + provenance |
| `config/generate.py` (internal) | write the hybrid `.opencode/opencode.jsonc` |

## Idempotency & preservation

- Re-running the generator MUST converge: it regenerates `.opencode/opencode.jsonc`
  deterministically and **preserves** an existing differing file (shows a diff and
  stops) unless `--apply`.
- Never overwrite a developer's hand-edited config or `pm-instructions.md`.
- Record the source ADOS git SHA (`.opencode/ados-provenance.txt`) so a later
  update knows what it is upgrading from.

## See also

- [`../constitution.md`](../constitution.md) — Working Rule 6 (non-destructive
  installs) and the ADOS-provenance rule.
- [`python.md`](python.md) — the Runner, two-tier policy, and CLI layout.
