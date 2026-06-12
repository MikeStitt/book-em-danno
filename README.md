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
opencode. See [`SAMPLE_README.md`](SAMPLE_README.md) for the full model.

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
kind     = "ollama"
base_url = "http://host.docker.internal:11434/v1"
num_ctx  = 32000

[backends.cloud]
kind     = "cloud"
provider = "anthropic"            # API keys stay in the env, never in this file

[models.gemma4]
backend   = "ollama"
tag       = "gemma4:26b"          # local models MUST be tool-capable (gemma3:1b is NOT)
tool_call = true

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
tool-call is unusable for an agent (keep `num_ctx ≈ 32000`).

## Development

- **Gate:** `ninja check` = `ruff check` + `ruff format --check` + `mypy` +
  `pytest` (fast suite). Run the live tests with `uv run pytest -m slow` (they skip
  cleanly when Docker/Ollama are down).
- **Layout:** `src/book_em_danno/` — `config/` (schema, loader, generator),
  `core/exec.py` (the advise-by-default `Runner`), `commands/` (doctor, ollama,
  sandbox, tools, install), `cli.py`.
- **Releasing:** the version reported by `danno --version` comes from
  `pyproject.toml`. `CHANGELOG.md` is generated from conventional commits by
  [`git-cliff`](https://git-cliff.org) (`brew install git-cliff`,
  `git cliff -o CHANGELOG.md`). Cut a release by bumping the version, regenerating
  the changelog, then pushing a `vX.Y.Z` tag — the tag push triggers
  [`release.yml`](.github/workflows/release.yml), which publishes the GitHub
  Release from the changelog. Full steps in
  [`.specify/memory/parts/shared.md`](.specify/memory/parts/shared.md).

## Where the docs live

- [`docs/ux-requirements.md`](docs/ux-requirements.md) — the reconciled command
  surface, network model, and `danno.toml` schema (the design-of-record).
- [`.specify/memory/constitution.md`](.specify/memory/constitution.md) — the
  authoritative development practices, with per-work-type detail in
  [`.specify/memory/parts/`](.specify/memory/parts/).

## License

MIT — see [LICENSE](LICENSE).
