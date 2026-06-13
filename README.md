# book-em-danno

**`danno`** is a Python CLI that declaratively provisions an [OpenCode](https://opencode.ai)
**hybrid local/cloud model runtime inside a Docker sandbox**, driven by a single
`danno.toml`. Cheap, high-volume agents run on local [Ollama](https://ollama.com)
models on your own machine; high-stakes agents run on a cloud model. From one file
`danno` writes the OpenCode config, pulls the local models, installs a catalog of
agentic tools (including [ADOS](https://github.com/juliusz-cwiakalski/agentic-delivery-os)),
and creates a Docker Desktop microVM sandbox wired to host Ollama.

Everything is **transparent and non-destructive**: by default `danno` *advises* —
it prints the exact copy-paste commands it would run. `--dry-run` previews
without touching anything; `--apply` executes.

## Install

```bash
uv sync            # install danno + its locked deps
uv run danno --help
uv run danno --version
```

`danno` is the entry point (`book-em-danno` is an alias).

## Getting started

### 1. Preflight

```bash
uv run danno doctor
```

A read-only PASS/FAIL/WARN checklist with copy-paste fixes: Python, git, the
Docker daemon, the `docker sandbox` subcommand, Ollama (installed, reachable, a
model pulled), and a WARN if Ollama is bound to loopback only (unreachable from
the sandbox VM).

### 2. Preview, then provision

```bash
# preview everything — writes and runs nothing
uv run danno --dry-run install --target ./my-project

# default: write the config we own, PRINT the host/Docker commands to run yourself
uv run danno install --target ./my-project

# actually do it: write config, pull models, create + wire the sandbox
uv run danno --apply install --target ./my-project
```

`install` runs the full happy path in order — validate `danno.toml` → write
`.opencode/opencode.jsonc` → ensure Ollama models → install tools → create the
sandbox — then prints the launch command. It **stops before** launching the TUI.
Re-running is idempotent: an unchanged config is a no-op; a changed config shows a
diff and needs `--apply`.

### 3. Launch and operate the sandbox

The confusion-resistant flow is to `cd` into the project and omit `--target`: it
defaults to `.`, so danno derives the same sandbox name every time.

```bash
cd ./my-project
# launch the in-container OpenCode TUI, wired to host Ollama
uv run danno --apply sandbox start
uv run danno --apply sandbox shell      # bash in the VM
uv run danno --apply sandbox stop
uv run danno --apply sandbox rebuild    # recycle from scratch (agent home survives)
uv run danno sandbox ls                 # which sandbox maps to which project?
```

`--target ./my-project` works too. The sandbox name is `danno-<parent>-<dir>` (the
parent dir is included so same-basename checkouts and worktrees never collide), and
`sandbox ls` reads `~/.danno/sandboxes.json` to print each `name → target` plus live
status. OpenCode **only ever runs inside the sandbox** — never on your host.

### Where the agent's history lives (`[sandbox] agent_home`)

danno launches the agent **in your mounted repo** (so it sees `CLAUDE.md` and edits
land on the host) and gives each sandbox a durable **agent home** on the host —
chat history, settings, onboarding — that survives `rebuild`. One knob in
`danno.toml` chooses where it lives:

```toml
[sandbox]
agent_home = "per-project"   # per-project (default) | per-repo | shared | ephemeral
                             #   | "group:<name>" | "<host path>"
```

`per-project` (the default) gives each sandbox its own home keyed on the sandbox
name; `per-repo` shares one home across a repo's worktrees; `shared` is one home for
all sandboxes; `ephemeral` keeps it VM-local (wiped on rebuild). danno translates
the knob to `CLAUDE_CONFIG_DIR` for Claude and `XDG_CONFIG_HOME`/`XDG_DATA_HOME` for
opencode. See [Sandboxed agents: repo, agent-home, auth](#sandboxed-agents-repo-agent-home-auth)
for the full model.

#### Other agents (`--agent`)

`docker sandbox` ships prebuilt agents (`opencode`, `claude`, …). Pass `--agent`
to run a different one; non-default agents get their **own** sandbox
(`danno-<parent>-<dir>-<agent>`) so they coexist with the opencode sandbox.

```bash
uv run danno --apply sandbox start --target ./my-project --agent claude
```

**Claude Code auth** is read from danno's host environment and injected into the
sandbox through a chmod-600 `--env-file` (never on the command line). danno
prefers the subscription token; set one of:

```bash
# Max/Pro subscription (preferred, no per-token billing): mint a long-lived token
claude setup-token            # opens a browser; OAuth against your Max/Pro account
export CLAUDE_CODE_OAUTH_TOKEN=...

# or API billing (pay-per-token via the Console)
export ANTHROPIC_API_KEY=...
```

If neither is set, `sandbox start --agent claude` **fails loud** with the
`claude setup-token` hint rather than launching unauthenticated. The token is
re-injected on every `start`, so it survives `sandbox rebuild`.

## `danno.toml` quickstart

`danno.toml` is the single source of truth. See [`danno.toml.example`](danno.toml.example)
for a fully-commented template. The essentials:

```toml
[defaults]
default_agent = "pm"
profile       = "hybrid"          # hybrid | cloud-only | local-only

[backends.ollama]                 # local models (OpenAI-compatible provider)
kind           = "ollama"
base_url       = "http://host.docker.internal:11434/v1"
context_budget = 32000            # OpenCode's client-side window belief; see knobs below
output_limit   = 8192

[backends.cloud]
kind     = "cloud"
provider = "anthropic"            # API keys stay in the env, never in this file

[models.gemma4]
backend          = "ollama"
tag              = "gemma4:26b"   # local models MUST be tool-capable (gemma3:1b is NOT)
tool_call        = true
reasoning_effort = "none"         # disable the thinking trace; see knobs below

[models.sonnet]
backend = "cloud"
id      = "anthropic/claude-sonnet-4-6"

[agents]                          # agent -> model: high-stakes cloud, high-volume local
pm        = "sonnet"
architect = "sonnet"
runner    = "gemma4"
committer = "gemma4"

[[tools]]                         # imperative tools (see "Two install lanes" below)
name       = "ados"
source     = "https://github.com/juliusz-cwiakalski/agentic-delivery-os"
install_to = "sandbox"
```

### Ollama context & runtime knobs

danno translates two `[backends.ollama]` fields and one per-model field into the
generated `opencode.jsonc`. The shape was verified at the wire (Ollama 0.30.6,
opencode dev) — see [`docs/research/2026-06-12-ollama-thinking-and-opencode-passthrough.md`](docs/research/2026-06-12-ollama-thinking-and-opencode-passthrough.md):

- **`context_budget`** (backend) → `models.<tag>.limit.context` — OpenCode's
  *client-side belief* of the window. OpenCode uses it to trim/compact the
  conversation and show usage; it **does not** change what Ollama loads. It is *not*
  Ollama's real window: under the OpenAI-compatible `/v1` API a body `num_ctx` is
  **ignored** and Ollama loads the model at its **full** context (gemma4:26b and
  qwen3.6:27b are both 262144). The cost there is **RAM, not truncation** — at 262k,
  gemma4:26b ≈ 16.9 GiB (sliding-window attention → tiny KV) but qwen3.6:27b ≈ 31.5
  GiB. The real-window / RAM lever is an **Ollama Modelfile variant** (`num_ctx`
  baked in via `ollama create`), not an `opencode.jsonc` key.
- **`output_limit`** (backend) → `models.<tag>.limit.output` — tokens OpenCode
  reserves for the reply. Usable input is roughly `context_budget − output_limit`.
- **`reasoning_effort`** (per-model, ollama only) → `models.<tag>.options.reasoningEffort`
  — forwarded **raw into the `/v1` request body**, where Ollama honors
  `none`/`low`/`medium`/`high`. `"none"` disables the model's thinking trace: it's
  **faster for high-volume local agents** and **sidesteps the opencode `#21903`
  hang** (opencode spins forever when an Ollama model returns a generic `reasoning`
  field — which gemma4:26b does emit by default). Omit it to forward nothing.
  gpt-oss-style models reject `"none"` — use `low`/`medium`/`high` for those.

There is deliberately **no `stream` or `thinking` knob**: opencode **always streams**
(`stream: true` is hardcoded), and a provider-level `thinking`/`stream`/`num_ctx`
never reached Ollama through `@ai-sdk/openai-compatible`.

### Editing the generated `opencode.jsonc`

danno generates `.opencode/opencode.jsonc` from `danno.toml` — **the happy path is
to edit `danno.toml`, not the generated file.** When you do hand-edit it:

- **Only `danno install` regenerates it.** The first run writes it automatically. On
  a re-run, danno compares the proposed output to what's on disk: if they differ it
  prints a unified diff and **refuses to write unless you pass `--apply`**. So plain
  `danno install` will *not* clobber your edits (it shows the diff);
  **`danno --apply install` overwrites them.** `--dry-run` never writes, and
  `sandbox`/`doctor`/etc. never touch the file.
- **It's already diff-friendly.** danno emits one key per line (no dense single-line
  objects), so edits and diffs stay readable.
- **Edits reach the sandbox on the next launch.** The project is bind-mounted
  read-write at the same path, so the in-container agent reads the same file. An edit
  takes effect on the next `danno sandbox start` (the agent reads it at startup) —
  **no `rebuild` or container recreation needed.**

### Two install lanes: `[[tools]]` vs `[[npm]]`

danno installs catalog tools two different ways, and a tool belongs in exactly one
lane:

- **`[[tools]]` — imperative.** A tool with its own installer. danno runs it (for
  ADOS: copies its agent/command `.md` defs project-local and runs its `--local`
  step in the target, with `cwd=<target>` and `ADOS_SOURCE_DIR` set). Use this for
  anything that is *not* an OpenCode npm plugin.
- **`[[npm]]` — declarative.** An [OpenCode npm plugin](https://opencode.ai/docs/plugins/).
  danno just lists it in the generated `opencode.jsonc` `"plugin"` array; **OpenCode
  (Bun) auto-installs it inside the sandbox at startup** — danno never clones or
  installs it on the host.

```toml
# A bare plugin (no options):
[[npm]]
package = "opencode-planner"

# A configured plugin, plus an optional in-container setup step:
[[npm]]
package = "@plannotator/opencode@latest"
setup   = ["curl -fsSL https://plannotator.ai/install.sh | bash"]   # run via docker sandbox exec, post-create

[npm.config]                       # renders as the [package, config] tuple OpenCode documents
workflow       = "plan-agent"
planningAgents = ["plan"]
```

That `[[npm]]` block generates this in `.opencode/opencode.jsonc`:

```jsonc
"plugin": [
  "opencode-planner",
  ["@plannotator/opencode@latest", { "workflow": "plan-agent", "planningAgents": ["plan"] }]
]
```

**End to end:** `danno --dry-run install` previews the `"plugin"` array and any
in-container `docker sandbox exec … bash -lc …` setup line; `danno --apply install`
writes the config and runs the setup step post-create; then `danno sandbox start`
launches OpenCode, which installs the plugins in-sandbox on first run. A `package`
with no `config`/`setup` is the minimum — `config` and `setup` are both optional.

## Network model (Docker sandbox)

The agents run in a Docker **microVM** — its own kernel, filesystem, and network.
Only the target project is mounted in; the rest of your Mac's filesystem is
invisible to them. Egress is governed by the sandbox proxy, which `danno sandbox`
configures as **`--policy allow --allow-host localhost:11434`**.

```text
                  ┌──────────────────── your Mac (host) ────────────────────┐
  internet  ◀─allow─▶                                   Ollama :11434 (0.0.0.0)
  cloud API ◀─allow─▶   Docker microVM ── allow ───────▶ (agent dials
                    │     OpenCode + agents                host.docker.internal,
                    │        │                             proxy rewrites→localhost)
  your LAN  ──DENY──│        └─ project mount (rw)                              │
  other host ports ─DENY                                                       │
                    └──────────────────────────────────────────────────────────┘
```

**What the sandbox allows and denies** (verified empirically):

| Target | Policy |
| --- | --- |
| Public **internet** (any domain) | ✅ allow — research, npm/pip, cloud model APIs |
| **Host Ollama** at `host.docker.internal:11434` | ✅ allow — the single host hole |
| **Other host services** (any other localhost port, e.g. SSH) | ❌ deny |
| **Your LAN / local network** (10.x, 172.16.x, 192.168.x) | ❌ deny |
| **Host filesystem** outside the mounted project | ❌ not present (microVM) |

**Why the allow-rule is `localhost:11434`, not `host.docker.internal`:** the proxy
rewrites `host.docker.internal` → `localhost` before matching the allow-list, so
the rule must name `localhost:11434`. The agent's config baseURL still uses
`http://host.docker.internal:11434/v1` — that's the address it dials.

**Prerequisite:** host Ollama must listen on `0.0.0.0` (`OLLAMA_HOST=0.0.0.0:11434`),
not the default `127.0.0.1`, or the VM can't reach it. **Local models must be
tool-capable** — every agent uses tools, and a model like `gemma3:1b` that cannot
tool-call is unusable for an agent (keep `context_budget ≈ 32000`).

## Sandboxed agents: repo, agent-home, auth

danno runs your coding agent — **Claude Code** or **opencode** — inside an isolated
Docker Desktop microVM, wired to your local Ollama models and your repo. You get a
real agent on your code without giving it your laptop. The whole system is **three
layers**; once you see them, everything else follows.

```
┌──────────────────────────────────────────────────────────────────────┐
│  YOUR MAC (host)                                                       │
│                                                                        │
│   ~/work/acme/                ← your repo (the "workspace")            │
│     ├── src/ …                                                         │
│     ├── CLAUDE.md             ← ① REPO layer: agent instructions       │
│     └── .claude/  /.opencode/    (committed, shared, travels w/ git)   │
│                                                                        │
│   ~/.danno/agent-home/        ← ② AGENT-HOME layer: chat history,      │
│     └── danno-work-acme-claude/   settings, onboarding (per sandbox)   │
│                                                                        │
│   $CLAUDE_CODE_OAUTH_TOKEN    ← ③ AUTH layer: a token in your env      │
└───────────────┬───────────────────────────────────────────────────────┘
                │  danno mounts ① + ②, injects ③, launches the agent
                ▼
┌──────────────────────────────────────────────────────────────────────┐
│  SANDBOX microVM  (disposable; `rebuild` wipes everything inside it)   │
│   /Users/you/work/acme   ← ① mounted at the SAME path, read-write      │
│   agent home (relocated to ②)  ← survives rebuild because it's on host │
│   token in env only      ← ③ never written to the VM's disk            │
└──────────────────────────────────────────────────────────────────────┘
```

| Layer | What's in it | Where it lives | Survives `rebuild`? |
|---|---|---|---|
| ① **Repo** | code + agent instructions (`CLAUDE.md`, `.claude/`, generated `.opencode/`) | your repo on the host, mounted in | ✅ (it's your repo) |
| ② **Agent home** | chat history, settings, onboarding/theme | a host folder, one per sandbox | ✅ |
| ③ **Auth** | the API/subscription token | your shell env → injected per launch | ✅ (re-injected) |

The microVM itself is **disposable**. Nothing important should live only inside it.
All three layers are durable: ① is your repo, ③ is re-injected each launch, and ②
lives in a host folder keyed by `agent_home` (configured under `[sandbox]`, see the
[agent_home quickstart](#where-the-agents-history-lives-sandbox-agent_home) above).

### The scenario: one repo, several worktrees

Say you keep a main checkout plus two `git worktree` siblings on different branches:

```
~/work/acme           branch: main
~/work/acme-login     branch: feature/login      (git worktree)
~/work/acme-billing   branch: feature/billing    (git worktree)
```

Each directory is a **separate workspace**, so danno gives each its **own sandbox**
and its **own agent home**. Sandbox names are `danno-<parent>-<dir>[-<agent>]` — the
parent dir is included so same-basename checkouts (and worktrees) never collide:

| Directory | Sandbox | Agent home (`per-project`) |
|---|---|---|
| `~/work/acme` | `danno-work-acme-claude` | `~/.danno/agent-home/danno-work-acme-claude/` |
| `~/work/acme-login` | `danno-work-acme-login-claude` | `~/.danno/agent-home/danno-work-acme-login-claude/` |
| `~/work/acme-billing` | `danno-work-acme-billing-claude` | `~/.danno/agent-home/danno-work-acme-billing-claude/` |

What you get:

- **Shared instructions, separate minds.** `CLAUDE.md` and `.claude/commands/` are
  committed, so all three branches inherit the same project rules via git. But each
  agent has its **own chat history and todos**, scoped to its branch. The login
  agent never sees the billing agent's conversation.
- **Run them at once.** Three sandboxes, three agent homes — no two processes write
  the same history file, so parallel work can't corrupt state.
- **Rebuild one freely.** `rebuild` on `acme-login` resets only that VM; its history
  persists on the host, and the other two are untouched.

### Why separate "User Global" is the default (and the best practice)

In normal use, Claude Code keeps one global home (`~/.claude`) shared across every
project on your machine. In danno, each sandbox gets its **own**. That is deliberate:

1. **It matches how you actually work.** A worktree exists to isolate a branch's
   changes; isolating that branch's *agent context* is the same instinct. No
   cross-branch memory bleed.
2. **Security.** A sandbox can only ever see its own home. A prompt-injection or a
   malicious dependency in one repo can't read another project's history — and can
   never touch your real `~/.claude` (it isn't mounted at all).
3. **Reproducibility.** A sandbox's behavior is a function of its repo + its own home.
   No invisible global state leaking in from unrelated projects.
4. **No corruption.** Parallel agents never contend over one history db.

To make every danno sandbox share a single home, set `agent_home = "shared"`. Reach
for it only when you want one continuous memory across all your work; the trade-offs
are the exact inverse of the four points above. (Never choose to share your real
`~/.claude` — see Security notes.)

### Sharing a home across a *set* of projects (the middle ground)

Between "every sandbox isolated" and "everything shares one brain" is the common real
case: **a family of related checkouts that should share a home, while staying private
from the rest of your work.** The rule is always the same — *`agent_home` is an
identity key; equal keys share a home* — so you just choose how the key is set.

**Worktrees of one repo → `per-repo` (zero config).** Every `git worktree` shares one
git dir (`git rev-parse --git-common-dir`), so danno keys the home on it automatically:

```toml
[sandbox]
agent_home = "per-repo"   # acme, acme-login, acme-billing share ONE home; other repos don't
```

**Any arbitrary set → name a group.** When the projects aren't worktrees of one repo
(or live anywhere on disk), give them a shared label:

```toml
[sandbox]
agent_home = "group:acme"   # → ~/.danno/agent-home/groups/acme/ — same name = same home
```

**Pick the folder yourself → an explicit path** at a midpoint:

```toml
[sandbox]
agent_home = "~/work/acme/.shared/agent-home"
```

**Avoiding repetition (`danno.workspace.toml`).** Rather than copy the line into every
toml, drop a workspace file at the midpoint and let children inherit it; a *relative*
`agent_home` resolves against that midpoint directory:

```
~/work/acme/
├── danno.workspace.toml   ← [sandbox] agent_home = ".shared/agent-home"
├── main/      (worktree)  ─┐
├── login/     (worktree)   ├─ inherit the parent; all resolve to ONE home
└── billing/   (worktree)  ─┘   at ~/work/acme/.shared/agent-home/
```

danno walks up from `--target` to the nearest `danno.workspace.toml` and inherits its
`[sandbox]`. (Inheritance covers *nested* layouts; sibling checkouts use `per-repo` or
`group:` instead.)

| You have… | Use | Result |
|---|---|---|
| Worktrees of one repo | `per-repo` | shared across worktrees, isolated from other repos — automatic |
| A named set anywhere | `group:<name>` | shared by every toml with that name |
| A specific folder in mind | `"<path>"` | shared by every toml pointing there |
| A nested tree, DRY | `danno.workspace.toml` + relative path | one declaration, inherited downward |

### opencode: same story, different drawers

opencode follows the **same three layers** — only the agent-home plumbing differs, and
danno hides it for you.

| | Claude Code | opencode |
|---|---|---|
| ① Repo config | `CLAUDE.md`, `.claude/` (you commit it) | `.opencode/opencode.jsonc` (**danno generates it from `danno.toml`**) |
| ② Agent home | one dir: `~/.claude/` (+`~/.claude.json`) | XDG dirs: `~/.config/opencode/`, `~/.local/share/opencode/` (sessions in a sqlite `opencode.db`) |
| ② Relocated by | `CLAUDE_CONFIG_DIR` | `XDG_CONFIG_HOME` + `XDG_DATA_HOME` |
| ③ Auth | `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` in env | local Ollama needs none (baked `baseURL`); cloud providers via env keys |

You set the **same** `agent_home` knob; danno translates it for both agents. The one
conceptual difference: with Claude you *write* the repo config (`CLAUDE.md`); with
opencode danno *generates* it (`.opencode/opencode.jsonc`) from `danno.toml`, so you
edit `danno.toml`, not the generated file (see
[Editing the generated `opencode.jsonc`](#editing-the-generated-opencodejsonc)).

### Security notes

- danno mounts **only your repo** (and, with `per-project`/`shared`, a dedicated
  agent-home folder). Your real `~/.claude` is **never** mounted — your global
  credentials, MCP secrets, and every project's history stay off the sandbox.
- Auth is injected as an env token through a `chmod 600` file that's deleted right
  after launch. It never lands on the VM's disk.
- Don't `docker sandbox save` a VM whose agent home holds credentials — that bakes
  secrets into an image.

### Quick reference

| You want… | Do this |
|---|---|
| Per-branch agents that don't share memory | default (`agent_home = "per-project"`) |
| Worktrees of one repo to share a home | `agent_home = "per-repo"` |
| A named set of projects to share a home | `agent_home = "group:<name>"` |
| A specific shared folder you pick | `agent_home = "<path>"` |
| One assistant with one memory everywhere | `agent_home = "shared"` |
| A throwaway agent, no persistence | `agent_home = "ephemeral"` |
| Project rules every agent follows | commit `CLAUDE.md` / edit `danno.toml` (opencode) |
| Change auth | `export` a token in your shell; never in `danno.toml` |
| Start fresh but keep history + rules | `danno --apply sandbox rebuild` |

## Development

- **Gate:** `ninja check` = `ruff check` + `ruff format --check` + `mypy` +
  `pytest` (fast suite). Run the live tests with `uv run pytest -m slow` (they skip
  cleanly when Docker/Ollama are down).
- **Layout:** `src/book_em_danno/` — `config/` (schema, loader, generator),
  `core/exec.py` (the advise-by-default `Runner`), `commands/` (doctor, ollama,
  sandbox, tools, install), `cli.py`.
- **Releasing:** automated — you never bump the version, write the changelog, or
  push a tag by hand. The version reported by `danno --version` comes from
  `pyproject.toml`, and `CHANGELOG.md` is generated from conventional commits by
  [`git-cliff`](https://git-cliff.org). To cut a release, run the
  [`release-prepare`](.github/workflows/release-prepare.yml) workflow and merge the
  `chore(release): vX.Y.Z` PR it opens;
  [`release.yml`](.github/workflows/release.yml) then tags and publishes the GitHub
  Release. Full process, prerequisites, and caveats in
  [`plans/releasing.md`](plans/releasing.md).

## Where the docs live

- [`docs/ux-requirements.md`](docs/ux-requirements.md) — the reconciled command
  surface, network model, and `danno.toml` schema (the design-of-record).
- [`.specify/memory/constitution.md`](.specify/memory/constitution.md) — the
  authoritative development practices, with per-work-type detail in
  [`.specify/memory/parts/`](.specify/memory/parts/).

## License

MIT — see [LICENSE](LICENSE).
