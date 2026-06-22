# danno sandbox agents — what they are and how to use them

> **Living document / intermediate guide (2026-06-22).** `.docs/` is **exempt from
> markdown/format quality checks** — this is a how-to + knowledge dump, not a spec.
> danno-specific behaviour is cited to `src/…:line` so it's verifiable. The *roster*
> of prebuilt agents is owned by Docker's `docker sandbox`, not danno, and moves —
> **re-verify with `docker sandbox create --help` before relying.** Companions:
> [`user-experience-elephant.md`](user-experience-elephant.md),
> [`connecting-claude-code-to-models-investigation.md`](connecting-claude-code-to-models-investigation.md),
> [`agents-and-cloud-backend-refactor.md`](agents-and-cloud-backend-refactor.md).

## Mental model (read this first)

danno's job is to stand up an **isolated Docker Desktop microVM ("sandbox")**, mount
your project inside it, wire it to host Ollama / agent auth, and then **launch a
coding agent inside that VM** so a runaway build or command can't touch your host.

The agent is one of Docker's **prebuilt sandbox agents**. danno does **not** ship or
allowlist agents — `--agent <name>` is forwarded verbatim to
`docker sandbox create … <name>` (`src/book_em_danno/commands/sandbox.py:262`), and the
container binary danno execs *is* that name (`sandbox.py:566`). So "which agents
exist" is whatever your `docker sandbox` provides.

Two of those agents are **first-class in danno** (it tailors their config/auth);
the rest are **pass-through** (danno provisions the sandbox and runs them, but you
supply their auth yourself). See the support tiers below.

## The roster

Verified prebuilt agents (danno memory `docker-sandbox-agents.md`; re-verify per the
caveat above): `cagent  claude  codex  copilot  gemini  kiro  opencode  shell`.

| Agent | What it is | danno support |
|---|---|---|
| **opencode** | [OpenCode](https://opencode.ai) — the open-source terminal AI coding agent danno is built around. The local-Ollama + cloud **hybrid** model map and plugins are configured here. | **First-class & default.** The only agent whose config danno generates (`.opencode/opencode.jsonc` from `danno.toml`). |
| **claude** | [Claude Code](https://claude.com/claude-code) — Anthropic's official CLI coding agent. | **First-class.** danno wires its auth + relocates its config home + pre-seeds onboarding. |
| **codex** | OpenAI's **Codex CLI** — OpenAI's terminal coding agent. | Pass-through (bring your own auth via `--env`). |
| **copilot** | **GitHub Copilot CLI** — GitHub's terminal coding agent. | Pass-through. |
| **gemini** | **Gemini CLI** — Google's open-source terminal AI agent. | Pass-through. |
| **cagent** | Docker's **cagent** — an open-source multi-agent runtime (agents declared in YAML). | Pass-through. |
| **kiro** | **Kiro** — AWS's spec-driven agentic dev tool. | Pass-through. |
| **shell** | **Not an AI agent** — a plain `bash` prompt inside the sandbox VM. Run builds/tests/tools in isolation, or use it as a base when you bring your own tooling. | Pass-through (no auth/config wiring needed). |

> Why only two are "first-class": danno's reason to exist is the **opencode hybrid +
> sandbox** wiring (see `user-experience-elephant.md`), and **claude** is the obvious
> alternate runtime, so danno tailors both. The others run fine in the sandbox; danno
> just doesn't know their config/auth conventions, so you pass those in.

## What each non-native agent is (researched 2026-06-22)

> Web-researched; sources dated below — **re-verify, these tools change monthly.** The
> one axis that matters for danno is **the local-Ollama hybrid**: can the agent be
> pointed at an arbitrary **OpenAI-compatible** endpoint, so cheap/high-volume agents
> run on your Mac's Ollama while heavy-reasoning ones use cloud? That split is danno's
> entire reason to exist (`user-experience-elephant.md`). Hybrid verdicts:
> **codex YES · cagent YES · copilot PARTIAL · gemini PARTIAL · kiro NO.**

### codex — OpenAI Codex CLI
- **What:** OpenAI's open-source terminal coding agent (`openai/codex`; open-sourced Apr 2025, now mostly Rust). Reads/edits/runs code in the working dir; reads `AGENTS.md`.
- **Hybrid — YES (native).** Define a provider in `~/.codex/config.toml`:
  `[model_providers.ollama-local]` with `base_url = "http://localhost:11434/v1/"`, then
  `model_provider = "ollama-local"` (or pick per `--profile`). Cloud + local providers
  coexist and you switch by profile — **structurally the same as opencode's provider
  model.** ⚠ Known bug: the *built-in* `ollama` provider id has ignored `base_url` in
  some versions (`openai/codex` #1734, #8240) — define your **own** `[model_providers.*]`.
- **Auth:** ChatGPT sign-in (Plus/Pro/…) OAuth, or `OPENAI_API_KEY`; a custom provider names its key var via `env_key`.
- **Config:** `~/.codex/config.toml` (TOML; `$CODEX_HOME` overrides; profiles select with `--profile`).
- Sources: developers.openai.com/codex/config-reference · docs.ollama.com/integrations/codex

### copilot — GitHub Copilot CLI
- **What:** GitHub's terminal agent (`github/copilot-cli`; GA Feb 2026; open-source status unconfirmed). Reads `AGENTS.md`, `.github/copilot-instructions.md`, even `CLAUDE.md`/`GEMINI.md`.
- **Hybrid — PARTIAL.** BYOK via **env vars** (added Apr 2026):
  `COPILOT_PROVIDER_BASE_URL=http://localhost:11434` (+ `COPILOT_MODEL`, optional
  `COPILOT_PROVIDER_TYPE`/`_API_KEY`) points it at Ollama with no key. But there's **no
  named multi-provider config** to switch local↔cloud in one session, and choosing a
  custom provider **disables GitHub-backed features** (`/delegate`, GitHub MCP, code
  search). So local-OR-cloud per launch, not a seamless hybrid.
- **Auth:** GitHub Copilot subscription; token order `COPILOT_GITHUB_TOKEN`→`GH_TOKEN`→`GITHUB_TOKEN`→keychain OAuth→`gh`. Classic `ghp_` not supported. BYOK needs no GitHub auth.
- **Config:** credentials in the OS keychain (service `copilot-cli`); fallback `~/.copilot/config.json`; **provider settings are env-var-only** (no config file).
- Sources: docs.github.com/…/copilot-cli/…/use-byok-models · …/authenticate-copilot-cli

### gemini — Google Gemini CLI
- **What:** Google's open-source (Apache-2.0) terminal agent (`google-gemini/gemini-cli`); Gemini models, Google Search grounding, MCP.
- **Hybrid — PARTIAL (proxy only).** Speaks only the **Gemini wire protocol**. The one
  lever is `GOOGLE_GEMINI_BASE_URL`, so reaching Ollama needs a **translating proxy**
  (LiteLLM impersonating the Gemini API) — you can't point straight at `:11434/v1`.
  Native OpenAI-compatible support is requested but not shipped (#23385); the override
  is fragile with cached Google auth (#15430). Third-party forks add real Ollama support.
- **Auth:** Google OAuth login, or `GEMINI_API_KEY`, or Vertex (`GOOGLE_GENAI_USE_VERTEXAI` + project/location).
- **Config:** `~/.gemini/settings.json` (JSON); env loadable from `.gemini/.env`.
- Sources: geminicli.com/docs/reference/configuration · docs.litellm.ai/…/litellm_gemini_cli

### cagent — Docker's multi-agent runtime
- **What:** Docker's open-source (Apache-2.0) **declarative multi-agent runtime/CLI**
  (`docker/cagent`, rebranding toward `docker agent`). **Not a single coding agent** —
  you declare a *graph* of agents + models + MCP toolsets in one YAML and run
  `cagent run file.yaml`.
- **Hybrid — YES (first-class; the closest analog to opencode).** Providers include
  `ollama` (default `base_url http://localhost:11434/v1`, no key), `dmr` (Docker Model
  Runner — local, no key), and `openai`+`base_url` for any OpenAI-compatible endpoint.
  You bind different sub-agents to local vs cloud models **in one file** — exactly the
  hybrid shape.
- **Auth:** per-provider env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, …); `token_key:` names the env var; local providers need none.
- **Config:** a YAML agent file passed to `cagent run`; no fixed default-path convention found.
- Sources: github.com/docker/cagent · docs.docker.com/ai/cagent/reference/config

### kiro — AWS Kiro CLI
- **What:** AWS's proprietary **spec-driven** agentic tool (successor to the Amazon Q
  Developer CLI); ships as both an IDE and a **real headless CLI**
  (`kiro-cli chat --no-interactive`). Bedrock-backed.
- **Hybrid — NO.** Models are a fixed Kiro-hosted/Bedrock roster chosen from a dropdown;
  **no custom endpoint, no Ollama, no BYO key.** Cloud-only. Headless mode also requires
  a **paid-tier** `KIRO_API_KEY`.
- **Auth:** AWS Builder ID / IAM Identity Center / Google / GitHub, or `KIRO_API_KEY` (paid).
- **Config:** `~/.kiro/settings/cli.json` (JSON); project `.kiro/` (steering markdown, MCP).
- Sources: kiro.dev/docs/cli/headless · kiro.dev/docs/models

### shell — not an agent
A plain `bash` prompt in the VM: no models, no auth, no config. Use it to run builds/tests/tools in isolation.

## Are they like opencode, or like claude?

No — they split into **two archetypes** along danno's hybrid axis:

- **opencode-like** (custom OpenAI-compatible endpoint → **hybrid-capable**): **codex**
  and **cagent** (and, weakly, **copilot** via per-launch env vars). These can run the
  cheap-local + cloud-heavy split that is danno's whole point. codex's TOML
  `[model_providers]`+profiles and cagent's YAML multi-model graph both mirror
  opencode's provider/agent model closely.
- **claude-like** (locked to the vendor's hosted models; you wire **auth, not
  endpoints**): **gemini** (Google), **kiro** (Bedrock), and **copilot** (GitHub) in its
  default mode. As with Claude Code, danno can realistically only wire their *auth* — it
  can't point them at local Ollama (kiro not at all; gemini only via a fragile proxy;
  copilot only by abandoning GitHub features).

Two more distinctions worth holding:

- **cagent is structurally different from all of them** — a multi-agent *framework*
  (you author the agent graph) rather than a ready-to-run single coding agent. Closer to
  "a config format that orchestrates sub-agents" than to opencode/claude.
- **Current-wiring nuance:** danno's `agent_env` "else" branch injects `OLLAMA_BASE_URL`
  + `XDG_CONFIG_HOME` for every non-claude agent (`sandbox.py:377-379`), but **only
  opencode actually reads those.** For codex/copilot/gemini/kiro those two vars are
  inert — each uses its own config/env conventions (above). So today a non-native agent
  is effectively just "sandbox + whatever auth you pass via `--env`."

## How danno wires each agent (the env matrix)

All wiring lands in a single **chmod-600 `--env-file`** bound into the sandbox exec —
secrets never hit the command line (`agent_env`, `sandbox.py:342-380`;
`_build_env_file`, `:383`). The shared session core (`_exec_session`, `:489`) is
identical for `start` (runs the agent) and `shell` (runs `bash`) — they differ only
in the container command.

| Agent | Auth danno injects | Config-home relocation (with a persistent agent-home) | Reads `opencode.jsonc`? |
|---|---|---|---|
| **opencode** | `OLLAMA_BASE_URL` → host Ollama (`sandbox.py:377`) | `XDG_CONFIG_HOME=<home>/config` (`:379`) | **Yes** — only for this agent (`:322`, `:513`) |
| **claude** | `CLAUDE_CODE_OAUTH_TOKEN` (preferred) **or** `ANTHROPIC_API_KEY`, from danno's host env; **fails loud** if neither is set (`:360-373`) | `CLAUDE_CONFIG_DIR=<home>` + onboarding/trust pre-seeded so no wizard blocks (`:374-375`, `seed_onboarding` `:512`) | No |
| **codex / copilot / gemini / cagent / kiro** | Falls into the `else` branch: gets `OLLAMA_BASE_URL` + `XDG_CONFIG_HOME` but **no bespoke auth** — supply the agent's own key via `--env` / `--env-file` (`:377-379`) | `XDG_CONFIG_HOME=<home>/config` | No |
| **shell** | `OLLAMA_BASE_URL` + `XDG_CONFIG_HOME` (the default branch) | `XDG_CONFIG_HOME=<home>/config` | No |

Two consequences worth internalising:

- **`danno.toml`'s model/backend/agent layer only affects `opencode`.** For every
  other agent it's unused — those agents use their own native config, which danno
  doesn't write. (`danno.toml` still drives the sandbox itself: `[sandbox]`,
  `[[tools]]`, `[[npm]]`, agent-home — those apply to *all* agents.)
- **opencode session persistence is partial.** Its config home is relocated onto the
  mounted agent-home, but its sqlite **data** dir is deliberately left VM-local
  (virtiofs can't honor sqlite WAL → Drizzle crash), so chats persist across
  stop/start but **reset on `rebuild`** (`sandbox.py:353-358`).

## Naming & coexistence

Sandbox names derive from the project's parent+own dir: `danno-<parent>-<dir>`. The
**default `opencode`** agent keeps the bare name; every other agent gets a
**`-<agent>` suffix** so multiple agents on the same project coexist as separate
sandboxes (`default_name`, `sandbox.py:39-50`). E.g. for `~/projects/elephant`:

- `opencode` → `danno-projects-elephant`
- `claude` → `danno-projects-elephant-claude`

Every `danno sandbox …` subcommand takes `--agent`, so it targets the right per-agent
sandbox. Standing in the project dir (`cd elephant`) and omitting `--target/--name`
recomputes the same name each time.

## How to use — commands & examples

### Default path (opencode)

```bash
# Preview the host/Docker/Ollama side effects (changes nothing):
danno install --target .
# Execute them (writes .opencode/opencode.jsonc, pulls Ollama models, installs
# tools, CREATES the opencode sandbox) — stops before the TUI:
danno install --target . --apply
# Launch opencode in the sandbox:
cd <project> && danno sandbox start
```

`danno install` has **no `--agent` flag** — it always provisions the default
(opencode) sandbox (`cli.py:83-92`). It's the opencode-specific path: config gen +
Ollama pulls + tools + opencode sandbox.

### Any other agent (claude, codex, …)

There's no `install` step for non-opencode agents; provision **and** launch in one go
via `sandbox start --apply` (it provisions when missing, then launches —
`_ensure_provisioned`, `sandbox.py:571`):

```bash
# Claude Code — export auth first (subscription token preferred):
claude setup-token            # then export the printed CLAUDE_CODE_OAUTH_TOKEN
#   …or: export ANTHROPIC_API_KEY=sk-ant-…
danno sandbox start --agent claude --apply

# A pass-through agent — bring its own auth via --env (lands in the 0600 env-file):
danno sandbox start --agent codex  --apply --env OPENAI_API_KEY=sk-…
danno sandbox start --agent gemini --apply --env GEMINI_API_KEY=…

# A plain sandboxed shell (no agent) — run builds/tests in isolation:
danno sandbox shell --agent shell --apply
```

> Pass-through auth: danno only knows claude's and opencode's auth conventions. For
> codex/copilot/gemini/cagent/kiro, name the env vars *that agent* expects with
> `--env KEY=VAL` (repeatable) or `--env-file <file>`; danno injects them verbatim.
> It will **not** fail loud for a missing pass-through key the way it does for claude
> — the agent itself will complain inside the VM.

### Forwarding args to the agent

Anything after `--` is passed verbatim to the agent binary (`launch` /
`sandbox.py:566`; `sandbox start` uses `allow_extra_args`):

```bash
danno sandbox start --agent claude -- --resume <session-id>
```

### Lifecycle (all take `--agent`)

```bash
danno sandbox shell   --agent claude   # bash in the SAME wiring as start (debug)
danno sandbox stop    --agent claude   # stop the VM
danno sandbox rebuild --agent claude   # wipe & re-provision (agent-home survives)
danno sandbox ls                       # list danno-tracked sandboxes
```

`shell` is environmentally identical to `start` (same `-w <project>`, same env-file)
— it just drops you at `bash` instead of the agent, so a tool you run by hand is
wired exactly as the agent would be (`_exec_session` SYNC REQUIREMENT, `:496-505`).

## Agent-home (shared across agents)

`[sandbox] agent_home` in `danno.toml` is the identity key for where an agent's
persistent config/history lives on the host; sandboxes whose key resolves to the same
path share one home. Forms: `per-project` (default, keyed on sandbox name) ·
`per-repo` · `shared` · `ephemeral` (VM-local, wiped on rebuild) · `group:<name>` ·
an explicit host path (`schema.py` `Sandbox`; `resolve_home`, `sandbox.py:146+`). The
home is relocated per-agent as in the env matrix above (claude → `CLAUDE_CONFIG_DIR`;
everything else → `XDG_CONFIG_HOME`).

## Should any non-native agent become first-class in danno?

"First-class" isn't binary — there are three escalating levels danno could offer:

1. **Pass-through (today):** danno provisions the sandbox; you supply auth via `--env`.
   Works for every agent, zero per-agent code.
2. **Auth-aware:** teach `agent_env` each agent's auth var (codex→`OPENAI_API_KEY`,
   copilot→`GITHUB_TOKEN`, gemini→`GEMINI_API_KEY`, kiro→`KIRO_API_KEY`) so users don't
   hand-pass them, with claude-style fail-loud. Cheap, but only a convenience.
3. **Fully first-class:** danno **generates the agent's config** from `danno.toml` to
   wire the Ollama hybrid — what it already does for opencode. This is danno's real
   value-add, and it only pays off where the hybrid is genuinely possible.

Recommendation by agent (the payoff of level 3 tracks the hybrid verdict):

| Agent | Hybrid | First-class worth it? | Why |
|---|---|---|---|
| **codex** | YES | **Strongest candidate (level 3)** | Its TOML `[model_providers]`+profiles map almost 1:1 to opencode's providers/agents. danno could generate `~/.codex/config.toml` from the *same* `[backends]`/`[models]`/`[agents]` — extending the hybrid thesis to a second agent. Cost: a 2nd generator + the built-in-`ollama` `base_url` workaround. |
| **cagent** | YES | Viable but **different shape (level 3, later)** | Hybrid-capable, but it's a framework: danno would generate an *agent-graph YAML* — a new artifact type, not a drop-in model map. Higher design cost; revisit after codex. |
| **copilot** | PARTIAL | **No** (maybe level 2) | BYOK is env-var/launch-time only and disables GitHub features; no real single-session hybrid. At most, add `GITHUB_TOKEN` auth convenience. |
| **gemini** | PARTIAL | **No** | Hybrid only via a LiteLLM proxy danno would have to run and maintain — fragile, large scope, and against the "never fabricate an external mechanism" rule (`parts/python.md`). |
| **kiro** | NO | **No** | Cloud-locked Bedrock roster + paid key; the local half is impossible, so danno's core value can't attach. |

**Bottom line:** don't first-class them wholesale. Keep **pass-through as the floor**.
If danno ever expands beyond opencode, **codex is the single agent worth fully
first-classing** — its provider/model wiring maps onto opencode's almost 1:1 (see
below), so the same `danno.toml` backend/model layer could drive its config too.
**cagent** is a plausible second (different artifact shape). **copilot/gemini/kiro**
are cloud-anchored or proxy-only — the pass-through floor (optionally plus a small
auth-aware convenience) is the right level; first-classing them would add ongoing
maintenance without delivering danno's hybrid. This mirrors the `danno.toml` evaluation:
danno's leverage *is* the local-Ollama hybrid, and that only attaches to agents with a
custom OpenAI-compatible endpoint.

### Why codex specifically: provider/model isomorphism (and its limit)

danno's internal model has exactly three concepts — **backend** (where/how to reach a
provider), **model** (a named tag on a backend), and **agent/role → model** (which
model a role uses). Each has a direct counterpart in *both* opencode.jsonc and codex's
`config.toml`, so danno's existing schema + `generate.py` resolver could emit codex
config from the same data with just a different serializer — no new concepts:

| danno.toml concept | opencode.jsonc | codex `~/.codex/config.toml` |
|---|---|---|
| `[backends.*]` (base_url, api_key_env) | `provider.<name>.options` (`baseURL`, `apiKey`) | `[model_providers.<id>]` (`base_url`, `env_key`) |
| `[models.*]` (backend + tag) | `provider.<name>.models.<tag>` → ref `<provider>/<tag>` | `model = "<tag>"` + `model_provider = "<id>"` |
| `[agents.*]` (role → model) | `agent.<name>.model` | `[profiles.<name>]` (`model` + `model_provider`) |

Concretely, this danno.toml:

```toml
[backends.danno-ollama]
kind = "ollama"
base_url = "http://localhost:11434/v1"
[models.qwen]
backend = "danno-ollama"
tag = "qwen3-coder-next"
[agents]
build = "qwen"
```

…which danno renders to opencode's `provider.danno-ollama` +
`agent.build.model = "danno-ollama/qwen3-coder-next"`, maps to codex as:

```toml
[model_providers.danno-ollama]
base_url = "http://localhost:11434/v1"
wire_api = "chat"
[profiles.build]
model = "qwen3-coder-next"
model_provider = "danno-ollama"
```

**The limit (why this is "isomorphic" only for the provider/model layer):** the
**agent dimension maps only loosely.** opencode agents are *concurrent roles in one
session* — a primary can invoke a subagent on a different model mid-run, which is how
"local model for high-volume subagents, cloud for the architect" works *within a single
session*. codex **profiles are mutually-exclusive launch-time selections**
(`codex --profile build`): codex runs as one profile at a time and has no concurrent
multi-agent graph. So `agent → profile` is an approximation. Minor mismatches too:
codex needs a `wire_api` opencode doesn't surface; opencode's per-agent markdown defs
(`.opencode/agent/*.md`) have no codex equivalent (codex uses `AGENTS.md` for
instructions, not per-agent model pins); and the built-in `ollama` provider id has had
a `base_url` bug, so danno would emit a custom `[model_providers.*]` id.

Net: the **provider/model wiring — the part that carries the local-vs-cloud hybrid
danno cares about — is a clean structural match**, so codex is the natural second
first-class target; the **concurrent multi-agent richness does not survive** the
mapping (it degrades to launch-time profiles).

## Gotchas

- **Wrong/typo agent name** isn't validated by danno — it's forwarded to
  `docker sandbox create`, which will error. The roster is Docker's.
- **`opencode.jsonc` warning** ("run `danno install` first") only fires for the
  opencode agent (`sandbox.py:322`); it's intentionally silent for the others.
- **Per-agent sandboxes are separate VMs** — switching `--agent` doesn't reuse the
  opencode sandbox; it provisions a suffixed one.
- **opencode chats reset on `rebuild`** (see persistence note above); `stop`/`start`
  preserve them.

## See also

- `README.md` "Sandboxed agents" / `danno.toml` quickstart — the user-facing summary.
- `.specify/memory/parts/ados-ollama.md` — what danno provisions and the network model.
- `sandbox.py` — the authoritative implementation (`agent_env`, `create`, `provision`,
  `launch`, `_exec_session`).
