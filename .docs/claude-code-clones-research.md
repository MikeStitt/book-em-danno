# Claude Code clean-room clones — research report

> **Status:** research notes for danno. Written 2026-06-23.
> **Scope:** the four projects the user named — `claw-code`, `Claurst`, `OpenClaude` /
> `Open-Claude-Code`, and `OpenClaw` — their derivatives, whether each is actively
> developed, and **how you configure agents into them**. Plus the danno angle: is there a
> *nexus* we could run inside a danno sandbox, could we adapt an *existing benchmark*, and
> **which are closest** to danno's design (headless + local Ollama + declarative agents)?
>
> **Source-quality caveat — read first.** The triggering event is *post-cutoff* (after this
> assistant's Jan 2026 knowledge), so everything here comes from live web search of GitHub
> repos and secondary write-ups. The secondary coverage is heavily AI-generated and the
> numbers (stars, forks, "fastest repo in history") are **inconsistent across sources** —
> treat all popularity/recency figures as *unverified*. Structural facts (language, install
> command, headless flag, config files, local-model env vars, derivative repo names) were
> read from the repos and are more reliable. Origin notes: [`.docs/claude-clean-room.md`](claude-clean-room.md),
> [`.docs/claude-clean-room-candidates.md`](claude-clean-room-candidates.md).

## 1. The leak, rationalized

Two true-but-confusing statements are circulating:

- **"Claude Code source leaked."** On **2026-03-31**, Anthropic reportedly shipped a
  source-map artifact in a public npm release (a missing `*.map` entry in `.npmignore`),
  exposing ~512k lines of unobfuscated TypeScript. This was a packaging accident, not an
  open-sourcing decision; the code stayed **proprietary** and Anthropic issued DMCA
  takedowns. Model weights and user data were *not* exposed.
- **"Claude Code is open source."** That's loose language for the *community* fallout:
  developers built **clean-room** rebuilds — reimplementing the observed architecture in a
  new language *without copying the proprietary text*. Clean-room rewrites sit in a
  deliberately-constructed legal gray zone (reverse-engineering-for-interoperability
  carve-outs are cited: US DMCA §1201(f), EU Software Directive Art. 6, UK CDPA §50B).

So: the official tool remains closed; the *clones* are independent open-source projects
that mimic its agent harness and are model-agnostic by design. ⚠️ The hype also attracted
malware (fake repos / npm packages) — stick to the canonical repos below.

## 2. The four candidates

Axes per project: **language · install · headless mode · local model (OpenAI-compatible) ·
how you configure agents · derivatives · maintenance**.

### Claw Code — `instructkr/claw-code` → `ultraworkers/claw-code`

The namesake of the post-leak wave, by **Sigrid Jin** (GitHub `devswha` / `@sigridjineth`).
Originally a clean-room **Python** port (via an AI codegen pipeline, "oh-my-codex"), now a
dual **Rust + Python** workspace. The canonical repo appears to have moved to
`ultraworkers/claw-code`, where it is described as an **"agent-managed museum exhibit"** —
planned, executed, verified and maintained by automated agents "with no human intervention."

- **Install:** build from source — `git clone … && cd claw-code/rust && cargo build --workspace`.
  (`cargo install claw-code` is a deprecated stub; the README warns against it.) Has a
  `claw doctor` diagnostic.
- **Headless:** `claw prompt "say hello"` (one-shot, non-interactive).
- **Local models — yes (good fit).** `export OPENAI_BASE_URL="http://127.0.0.1:11434/v1"` +
  placeholder `OPENAI_API_KEY`, *or* `export OLLAMA_HOST="http://127.0.0.1:11434"` (Claw then
  routes all models to the local OpenAI-compatible endpoint automatically). Model picked per
  call: `claw --model "qwen3:latest" prompt "…"`. Examples for llama.cpp (`:8080/v1`) and
  vLLM (`:8000/v1`) are documented, so a custom base URL like
  `http://host.docker.internal:11434/v1` should work the same way.
- **Configuring agents:** reads `.claude.json` / `.claw.json` and tracks parity via
  `PARITY.md` / `ROADMAP.md`, but **no per-agent/subagent definition schema, system-prompt
  override, or per-agent model routing is documented** — the agent-definition story is the
  thinnest of the group. Auth is API-key based (`ANTHROPIC_API_KEY`), not subscription login.
- **Derivatives / siblings:** `Yeachan-Heo/gajae-code`, `code-yeongyu/lazycodex`,
  `Yeachan-Heo/clawhip` — a Korean-developer cluster; claw-code itself is built *with*
  Gajae-Code / LazyCodex. **Benchmark sibling:** `devswha/claw-bench` (see §3). See the
  callout below for what the README points readers to.
- **Maintenance:** many commits, **no published releases**, autonomously maintained. Real but
  **novelty / uncertain longevity**.

#### What Claw Code's README points you to (the "real crab-powered harnesses")

Claw Code's README explicitly steers readers **away from Claw Code itself** ("museum
exhibit") toward the production projects in the same ecosystem. The important catch: these
are **not clean-room Claude Code clones** — they are **agent-harness *distributions* that
wrap an existing agent** (GitHub Codex or Claude Code), i.e. the "LazyVim-for-coding-agents"
pattern. So they're a *different category* from Claw Code/Claurst/open-claude-code: an
orchestration/skills layer on top of someone else's CLI rather than a from-scratch agent.

- **LazyCodex** — `code-yeongyu/lazycodex`. **TypeScript** distribution that packages **OmO
  (oh-my-openagent)** on top of **GitHub Codex**. Install `npx lazycodex-ai install`
  (`--no-tui --codex-autonomous` for headless setup), `npx lazycodex-ai doctor`. Ships
  "Discipline Agents" (Sisyphus, Hephaestus, Oracle, Librarian) into `~/.codex/agents/`,
  config in `~/.codex/config.toml`, category-based model routing in source. **Cloud-only —
  OpenAI GPT-5.x routing, no Ollama / OpenAI-compatible base URL.** Actively maintained
  (v4.13.0, 2026-06-22; 11 releases) by "Jobdori" (Sisyphus Labs).
- **Gajae-Code** (`gjc`) — `Yeachan-Heo/gajae-code`. **TypeScript** (+ some Rust/Python).
  An *external* harness that runs **alongside Claude Code or Codex CLI** (it doesn't embed).
  Install `bun install -g gajae-code` (prebuilt Linux/Win/macOS-arm64). Ships role agents
  (executor/architect/planner/critic) + workflow skills (deep-interview, ralplan, ultragoal,
  team) under `packages/coding-agent/src/...`; config `~/.gjc/config.yml`; **RPC mode** for
  subprocess workers. **No Ollama / OpenAI-compatible endpoint documented.** Active (v0.7.1;
  32 releases).
- **Broader `oh-my-*` / UltraWorkers ecosystem also linked:** `code-yeongyu/oh-my-openagent`
  (OmO, the engine LazyCodex wraps), `code-yeongyu/oh-my-codex` (OmX, the AI pipeline that
  originally generated Claw Code), `Yeachan-Heo/oh-my-claudecode`, `Yeachan-Heo/clawhip`.

**danno relevance:** these harnesses are interesting *prior art for declarative role-agent +
skill configuration*, but they **wrap cloud agents (Codex/Claude Code) and document no local
Ollama path**, so they're **off-thesis** for danno's local-model design — further from danno
than Claurst/Claw Code, not closer.

### Claurst — `Kuberwastaken/claurst`

A from-scratch **pure-Rust** terminal coding agent (single compiled binary). Started as a
clean-room reimplementation "from spec," now a TUI pair-programmer with a companion
("Rustle"), chat forking, memory consolidation, plugins, and an experimental managed-agent
mode. Emphasis on **zero telemetry** (your code stays local).

- **Install:** install script (`curl … install.sh | bash` → `~/.claurst/bin`), `npm i -g
  claurst` / `bun i -g claurst` (postinstall fetches the platform binary), prebuilt binaries
  (Win/Linux/macOS, x86_64 + aarch64), or `cargo build --release --package claurst`.
- **Headless:** `claurst -p "explain this codebase"` (one-shot). **Output-format flags are
  underdocumented** — verify `claurst -p --help` before relying on JSON output.
- **Local models — yes (good fit).** 30+ providers (Anthropic, OpenAI, Google, Copilot,
  **Ollama**, DeepSeek, Groq, Mistral, …). Config in `~/.claurst/settings.json`; auth via
  `claurst auth login`, `/connect` in the TUI, or env vars (e.g. `ANTHROPIC_API_KEY`).
- **Configuring agents:** project rules in an `AGENTS.md`; a **Manager-Executor
  "managed-agents"** mode (`/managed-agents`) that delegates execution to smaller/specialized
  models while a "Manager" keeps high-level logic; a plugin system; and **ACP (Agent Client
  Protocol)** support so editors can drive it with permission prompts routed through
  `session/request_permission`. Richer agent story than Claw Code, lighter than open-claude-code.
- **Derivatives:** none notable.
- **Maintenance: actively developed** — v0.1.5 Beta (≈2026-06-11), ~8 releases, recent
  features (`/share` to Gist, "Free Mode", `/goal` multi-turn objectives). Most polished
  *local-first* option.

### Open-Claude-Code — `ruvnet/open-claude-code` (the "OpenClaude" banner)

A **Node/TypeScript** ground-up rebuild of the Claude Code CLI by **ruvnet** (Reuven Cohen),
informed by an AI-powered decompilation of the published npm package ("ruDevolution"). It
deliberately **mirrors Claude Code's real CLI surface and architecture** (async-generator
agent loop, ~25 tools, MCP transports, permission modes, hooks, settings chain, sessions).

- **Install:** `npx @ruvnet/open-claude-code "…"` (no install) or `npm i -g
  @ruvnet/open-claude-code` → the **`occ`** command.
- **Headless:** `occ -p "…" --output-format {text,json,stream-json}` — **the closest to
  Claude Code's own `claude -p --output-format stream-json`**, which matters because danno's
  validator already speaks exactly that dialect.
- **Local models — NO (poor fit for danno's local thesis).** Documented providers are
  Anthropic / OpenAI / Google AI / Bedrock / Vertex via env vars; **no Ollama or custom
  base-URL support is documented**. Useful for *cloud-model* comparison only. (Default model
  `claude-sonnet-4-6`.)
  > **⚠️ CORRECTED 2026-07-02 (read from source, not the README):** this is wrong. occ's
  > `callOpenAI` honors `OPENAI_BASE_URL` (`v2/src/core/agent-loop.mjs:368`), so it **can**
  > drive Ollama and any OpenAI-compatible endpoint — the model name must be `gpt-`/`o1`/`o3`
  > prefixed (else it routes to the hardcoded Anthropic endpoint) plus a dummy
  > `OPENAI_API_KEY`. Only the *Anthropic* endpoint is truly hardcoded. occ's cloud is also
  > **multi-provider** (Anthropic + OpenAI + OpenAI-compat clouds + Gemini), not
  > Anthropic-only. Full detail:
  > [`open-claude-code-integration-research.md`](open-claude-code-integration-research.md) §5/§7.
- **Configuring agents — richest & most Claude-faithful.** Subagents are **Markdown files
  with YAML frontmatter**: custom system prompts, tool allow/deny (`--allowedTools` /
  `--disallowedTools`), permission modes (`default|auto|plan|acceptEdits|bypassPermissions|
  dontAsk`), `--max-turns`, hooks, skills, and **nested subagents** (the depth-5 nesting that
  also landed upstream in Claude Code, June 2026). This frontmatter-driven model is the one
  most compatible with danno's "write model assignment into the agent `.md` frontmatter" lever.
- **Derivatives / siblings:** part of ruvnet's broader ecosystem — **`ruflo` / `claude-flow`**
  (a multi-agent swarm meta-harness for Claude Code). Sibling tooling, not a fork of OCC.
- **Maintenance: actively developed** (the README advertises nightly decompile tracking).

### OpenClaw — `openclaw/openclaw` ⚠️ disambiguation: **not a coding clone**

This is almost certainly a **name collision**, and resolving it is half of what the user
asked for. `openclaw/openclaw` is **Peter Steinberger's "Molty"** — a *personal AI assistant
platform* you self-host, connecting to messaging channels (WhatsApp, Telegram, Slack,
Discord, Signal, iMessage), with voice and a live "Canvas." It is **not** a terminal
Claude-Code clean-room *coding* agent.

It is, however, very capable on the agent-config axis, which is why it shows up in searches:
workspace prompts (`AGENTS.md` / `SOUL.md` / `TOOLS.md`), a skills system
(`~/.openclaw/workspace/skills/<skill>/SKILL.md`), **multi-agent routing** (inbound
channels → isolated agent sessions), per-session sandboxing
(`agents.defaults.sandbox.mode: "non-main"`), simple model config
(`{ agent: { model: "<provider>/<model-id>" } }`), provider failover, and **Ollama provider
docs**. Node/TS, pnpm workspaces. By the metrics shown it is *much* larger and more active
than the coding clones.

**Takeaway:** the user's list conflates two things. **"OpenClaude" / "Open-Claude-Code"** =
the *coding* clone banner (canonically `ruvnet/open-claude-code`). **"OpenClaw"** = the
*messaging/personal-assistant* platform. Same crustacean pun, different category. For danno
(a coding-agent provisioner), OpenClaw is **out of scope** — though its Ollama-provider +
declarative-agent docs are worth a glance as prior art.

## 3. How they're benchmarked — and the danno angle

### The existing benchmark: `devswha/claw-bench`

The obvious "existing benchmark to adapt" — Sigrid Jin's suite comparing **Claw Code (Rust)
vs Claude Code (Node.js)** (+ optional Codex). But two things make it a **poor fit** to adopt
wholesale:

1. **It pivoted to runtime-overhead, not task-effectiveness.** The stable suite measures cold
   **startup** (hyperfine), **binary/install footprint** (`du`), and **idle RSS**
   (`/usr/bin/time -v`) — and reports dramatic wins for the Rust binary (e.g. ~73× faster
   startup, ~17× smaller binary, ~47× lower idle memory). The README states task-effectiveness
   benchmarking "**has moved to OpenBench**."
2. **The task-effectiveness scripts are heavyweight and cloud-only.** `bench-swebench.sh`
   (**SWE-bench Verified**), `bench-terminal.sh` (**Terminal-Bench 2.0**), `bench-polyglot.sh`
   (**Aider Polyglot**) exist but are experimental, need **~50GB (SWE) + ~20GB (Terminal)** of
   Docker images, and require **official binaries + API keys** — agent swap is via `env.sh`
   (`CLAW_BIN` / `CLAUDE_BIN` / `CODEX_BIN` / `API_KEY`) with **no local-model / Ollama path**.

For reference, the industry harnesses these clones target are **SWE-bench Verified**,
**Terminal-Bench 2.0**, and **Aider Polyglot** — all Docker-per-task and heavy.

### The nexus (what's common — and danno-shaped)

Across all the *coding* clones, two interfaces converge, and they are exactly the two danno
already uses:

1. **Headless one-shot:** `<bin> -p "<prompt>"` — `claude -p`, `claurst -p`, `occ -p`,
   `claw prompt`.
2. **OpenAI-compatible local backend at a custom base URL** — i.e. danno's existing
   sandbox→host wiring `http://host.docker.internal:11434/v1` (the `localhost:11434` egress
   hole danno already opens for opencode).

danno's validator already abstracts precisely this shape: "run a headless one-shot inside a
disposable sandbox, capture output, score side effects from the mounted workspace." See
[`src/danno_validator/driver.py`](../src/danno_validator/driver.py) (the
`opencode run --format json` / `claude -p --output-format stream-json` primitives),
[`sweep.py`](../src/danno_validator/sweep.py) (`run_tiers`), and the agent-agnostic oracles
[`level0.py`](../src/danno_validator/level0.py) / [`level1.py`](../src/danno_validator/level1.py)
/ [`level2.py`](../src/danno_validator/level2.py). A clone would slot in as a new
*agent-under-test* row alongside the existing `claude` baseline.

### Recommendation (for a future pass — out of scope this report)

Don't drag in claw-bench / SWE-bench / Terminal-Bench. If danno ever benchmarks a clone,
**add it as a new agent-under-test in danno's own L0→L1→L2 harness, pointed at host Ollama** —
lighter, already container-native, and on-thesis (local models). The clone needs only a thin
new driver primitive + an install step inside the sandbox (none of these are in the prebuilt
sandbox roster: claude/opencode/codex/copilot/gemini/cagent/kiro/shell).

## 4. Which are closest?

| Project | Local Ollama fit | Headless | Agent-config richness | Maintenance | Verdict for danno |
|---|---|---|---|---|---|
| **Claw Code** | ✅ `OPENAI_BASE_URL`/`OLLAMA_HOST` | `claw prompt` | ⚠️ thin (`.claw.json`) | maintained "museum exhibit" | Strong *local* fit; namesake of the benchmark ecosystem; longevity uncertain |
| **Claurst** | ✅ native Ollama, 30+ providers | `claurst -p` | ✅ managed-agents, ACP, plugins, `AGENTS.md` | ✅ active (v0.1.5 Beta) | **Best all-round local fit**; cleanest install |
| **open-claude-code** | ⚠️ yes via `OPENAI_BASE_URL` (corrected — see note below) | `occ -p --output-format stream-json` | ✅✅ markdown-frontmatter subagents (most Claude-faithful) | ✅ active | Closest to Claude Code's *CLI + agent model*; **also** reaches Ollama + OpenAI-compat clouds |
| **OpenClaw** | ✅ (but messaging platform) | n/a (not a CLI coding agent) | ✅✅ skills/routing | ✅✅ very active | **Out of scope** — different category (name collision) |

**Closest, by axis:**

- **To danno's local + headless design:** **Claw Code** and **Claurst** (both reach host
  Ollama over an OpenAI-compatible base URL and have a one-shot mode). Of the two, **Claurst**
  is the better bet — actively released, single-binary install, native Ollama, real agent
  config — with **Claw Code** as the namesake fallback (and the project the existing
  `claw-bench` benchmark orbits).
- **To Claude Code's CLI + declarative-agent model:** **open-claude-code** (`occ`,
  YAML-frontmatter subagents — the closest analogue to danno writing model assignment into
  agent `.md` frontmatter). Caveat: cloud-only, so it doesn't exercise danno's local thesis.
  > **⚠️ CORRECTED 2026-07-02:** *not* cloud-only — occ reaches Ollama and OpenAI-compatible
  > clouds via `OPENAI_BASE_URL` (see §5 of
  > [`open-claude-code-integration-research.md`](open-claude-code-integration-research.md)).
  > And the frontmatter-model analogue is weaker than it looks: occ **parses** per-agent
  > `model` but never dispatches it (`v2/src/tools/agent.mjs:62-77`), so danno's
  > frontmatter-model lever is inert for occ — drive the model via `-m`.
- **Out of scope:** **OpenClaw** (a personal-assistant platform, not a terminal coding clone).

## 8. occ fork prompt-divergence audit (added 2026-07-08)

> **Why:** occ ships a **one-sentence** system prompt (`"You are an AI coding assistant."`
> + merged CLAUDE.md + a truncated tool summary — `v2/src/core/system-prompt.mjs`), with no
> agentic-discipline scaffolding. In danno's triple (**harness × (model+config) × sandbox**)
> a harness's prompt is a first-class, benchmarkable lever. Question: has anyone in occ's
> fork network already built a richer prompt surface we could learn from or adopt into
> `MikeStitt/open-claude-code`? Method: enumerate all forks of `ruvnet/open-claude-code`,
> compare each against upstream `main`, keep those that (a) are ahead and (b) touch a
> **prompt-surface file**, then read the actual patch. Prompt-surface set (primary =
> prompt text/persona): `core/system-prompt.mjs`, `tools/agent.mjs`, `agents/{parser,loader,teams}.mjs`,
> `skills/loader.mjs`, `config/cli-args.mjs`, `core/agent-loop.mjs`, `index.mjs`; secondary
> (model-routing) = `optimize/cascade.mjs`, `config/{env,settings}.mjs`.

**Funnel (via `gh api .../compare`, run 2026-07-08):**
`161 forks → 6 ahead of upstream → 2 touch a prompt-surface file → 1 substantive prompt change.`
3 forks 404'd (deleted/renamed: `sadadonline17-oss`, `aaryav1421-cmd`, `bellyfat`). The 3
forks that are ahead but touch **no** prompt file are all the same chore —
`chore: update last-known Claude Code version to 2.1.x` (`engene163`, `leduykhuong-daniel/…-refactor`,
`liugh-dev`); `walderfranco` is a 1-commit non-prompt change. Script + raw results:
scratchpad `fork-audit/` (`run_compare.sh`, `compare_results.jsonl`, `processed.txt`).

**The one substantive fork — `codomium/CODE` (89 commits ahead; a rebrand of occ):**
It adds two exported functions to `system-prompt.mjs` — `buildWorkspaceSnapshot()` (a capped,
excluded-dir-aware **file-tree** string) and `buildWorkspaceContent()` (tree **+ contents of
priority files**: README, package.json/Cargo.toml/pyproject, entry points, CLAUDE.md, capped
at 8 KB/file, 64 KB total). The stated purpose, verbatim from the diff: *"intended for thinking
models (e.g. Kimi K2.5, DeepSeek R1) that cannot make live tool calls. By providing actual file
contents up front, the model can give accurate, project-specific answers without needing tool
access."* — i.e. it front-loads workspace context into the prompt to compensate for models that
won't/can't call tools. **This is directly relevant to danno's local-model case** and to the
narrate-then-stop / no-tool-call failures we saw: it's a *harness-side* mitigation (feed context
in) rather than a model fix. Worth reading as prior art if we ever add a "context-injection"
lever to `MikeStitt/open-claude-code`. (Caveat: `codomium/CODE` is an unrelated-looking rebrand,
last pushed 2026-06-13, no description — provenance unverified; treat as a code reference, not a
dependency.)

**Not a prompt change — `HEskandari/open-claude-code` (1 commit):** *"Add DeepSeek provider
support."* It touches `agent-loop.mjs`/`env.mjs`/`providers.mjs`, but the only prompt-adjacent
line is emitting the system prompt as a `{role:'system'}` message (OpenAI-style) for the new
provider — **model-routing/provider plumbing, not prompt-text divergence.** Secondary bucket.

**Bottom line:** across 161 forks, **nobody has meaningfully rewritten occ's thin system prompt
or personas.** The single relevant divergence is context-injection for tool-averse models
(`codomium/CODE`), not a richer instruction scaffold. So enriching occ's prompt (e.g. porting a
real Claude Code prompt — see §9) remains **greenfield** for us; there is no upstream fork to
adopt wholesale.

## 9. Reprocessors of `Piebald-AI/claude-code-system-prompts` (added 2026-07-08)

> **Why:** occ's prompt is threadbare; the closest thing to a spec for a *well-scaffolded*
> harness prompt is `Piebald-AI/claude-code-system-prompts` — the verbatim, per-version archive
> of the **real** Claude Code system prompt (~11.7k★, 1990 forks; "27 builtin tool descriptions,
> sub-agent prompts, utility prompts"). It exposes **no API** — it's markdown reference text.
> So the practical question for porting a real prompt into occ is: **has anyone reprocessed it
> into a cleaner or machine-consumable format?** Nets run 2026-07-08: (1) the 1990 direct forks
> filtered to 150 with their own commits, isolating **renamed** forks as the reprocess signal;
> (2) `gh search repos "claude code system prompt"`; (3) `gh search code "Piebald-AI/claude-code-system-prompts"`
> (consumers); (4) the archive's own companion tool. Raw output: scratchpad `piebald/`.

**Reprocessors worth using (structured / reformatted):**
- **`bl-ue/tweakcc-system-prompts`** — the archive **split into one `.md` per prompt**
  (`agent-prompt-explore.md`, `-plan-mode-enhanced.md`, `-security-review-slash.md`, …), curated
  for **tweakcc** and advertised ~48 KB smaller / 30% faster. *Best format for selectively porting
  individual prompts* — granular and named. (43★)
- **`skrabe/lobotomized-claude-code`** — reorganized **per model** (`system-prompts-opus-4-8/`,
  `-opus-4-7/`, `-fable-5/`) + `system-reminders/`, packaged as **minimal, de-bloated prompt
  overrides**. Effectively a ready-made "prompt override" product; useful if we want a *lighter*
  scaffold rather than the full thing. (73★)
- **`kn1026/cc`** — the whole prompt **flattened into a single 148 KB `claudecode.md`**. Most-starred
  derivative (711★); convenient for one-shot ingestion / diffing.
- **`lucas-flatwhite/claude-code-system-prompts`** — a `prompts/` dir + analysis README
  ("prompt architecture" documentation). Reference, lightly restructured. (155★)
- **Translations (reprocess → another language):** `Lionad-Morotar/…-cn`, `juzi0551/…-zh`,
  `cc1024201/…-zh`, `GuoShamin/…-zh` ("系统提示词中文整理、结构化拆解与源码映射"). Not useful for us
  directly but confirm the "reformat" pattern.
- **`Domoryonok/claude-code-system-prompts`** — a single README, prompts **extracted from the
  *decompiled binary* (v2.1.56)** — an independent extraction path, not a Piebald fork.

**Verbatim-but-raw (not clean formats):**
- **`mehdigreefhorst/claude-code-system-prompt-cli`** — ships `systemtools.js`, the **raw ~11 MB
  minified CLI bundle** with the real prompt inline (template literals intact, e.g.
  `You are an interactive CLI tool that helps users ${…}`). This is the *actual* source text with
  its interpolation vars, but unprocessed — useful as ground truth, not as a loadable format.
- **`jonathanmotif/claude-code-system-prompts-db`** — despite the `-db` name, it's a Piebald-branded
  mirror (`system-prompts/` + `tools/` dirs), **not** a database/structured export. Downgraded.

**Consumers (tools that ingest these prompts, not reformatters):**
- **`Piebald-AI/tweakcc`** — the archive's companion; **patches these strings inside the real
  Claude Code binary** (pairs with `bl-ue/tweakcc-system-prompts` above).
- **`DigitalCyberSoft/claude-proxy`** — an **HTTP proxy that rewrites Claude Code's system prompt**
  on the wire "to fix corner-cutting behavior." A different delivery mechanism for the same idea
  (inject a better prompt without forking the tool) — conceptually the closest to a danno-style
  harness lever.
- **`letta-ai/letta-code`** (`src/agent/prompts/`), `FlorianBruniaux/claude-code-ultimate-guide`,
  `hesreallyhim/awesome-claude-code`, `VILA-Lab/Dive-into-Claude-Code` — reference/consume the
  archive but don't reformat it.

**Bottom line for danno:** if we want to port a *real* Claude Code prompt into
`MikeStitt/open-claude-code`, the cleanest starting points are **`bl-ue/tweakcc-system-prompts`**
(granular per-prompt `.md`, easy to cherry-pick) or **`skrabe/lobotomized-claude-code`**
(per-model, de-bloated — closer to a local-model budget). No fork exposes a JSON/YAML API, so any
port is still a copy-and-adapt, but these save transcription from the raw archive. `claude-proxy`
is prior art for *injecting* a better prompt without forking the harness at all.

## Sources

Primary (repos):
- https://github.com/instructkr/claw-code · https://github.com/ultraworkers/claw-code
- https://github.com/ultraworkers/claw-code/blob/main/docs/local-openai-compatible-providers.md
- https://github.com/Kuberwastaken/claurst
- https://github.com/ruvnet/open-claude-code · https://github.com/ruvnet/open-claude-code/blob/main/README.md
- https://github.com/ruvnet/ruflo
- https://github.com/openclaw/openclaw · https://docs.openclaw.ai/providers/ollama
- https://github.com/devswha/claw-bench
- Claw Code's recommended harnesses / ecosystem: https://github.com/code-yeongyu/lazycodex · https://github.com/Yeachan-Heo/gajae-code · https://github.com/code-yeongyu/oh-my-openagent · https://github.com/code-yeongyu/oh-my-codex · https://github.com/Yeachan-Heo/oh-my-claudecode · https://github.com/Yeachan-Heo/clawhip

Secondary (treat as AI-amplified, numbers unverified):
- https://claw-code.codes/ · https://klymentiev.com/blog/claw-code-claude-source
- https://wavespeed.ai/blog/posts/what-is-claw-code/ · https://wavespeed.ai/blog/posts/claw-code-vs-claude-code/
- https://cybernews.com/tech/claude-code-leak-spawns-fastest-github-repo/
- https://layer5.io/blog/engineering/the-claude-code-source-leak-512000-lines-a-missing-npmignore-and-the-fastest-growing-repo-in-github-history/
- https://www.beankinney.com/512000-lines-one-night-zero-permission-the-claude-code-leak-and-the-legal-crisis-of-ai-clean-rooms/
- https://code.claude.com/docs/en/sub-agents (upstream subagent format, for comparison)

§8 occ fork audit (repos read via `gh api` 2026-07-08):
- https://github.com/codomium/CODE (workspace-context injection for thinking models — the one substantive prompt fork)
- https://github.com/HEskandari/open-claude-code (DeepSeek provider; routing-only)

§9 Piebald reprocessors (repos read via `gh api` / `gh search` 2026-07-08):
- https://github.com/Piebald-AI/claude-code-system-prompts (the archive) · https://github.com/Piebald-AI/tweakcc
- https://github.com/bl-ue/tweakcc-system-prompts · https://github.com/skrabe/lobotomized-claude-code
- https://github.com/kn1026/cc · https://github.com/lucas-flatwhite/claude-code-system-prompts
- https://github.com/mehdigreefhorst/claude-code-system-prompt-cli · https://github.com/Domoryonok/claude-code-system-prompts
- https://github.com/DigitalCyberSoft/claude-proxy · https://github.com/letta-ai/letta-code
- Translations: https://github.com/Lionad-Morotar/claude-code-system-prompts-cn · https://github.com/juzi0551/claude-code-system-prompts-zh · https://github.com/GuoShamin/claude-code-system-prompts-zh
