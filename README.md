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

```bash
# launch the in-container OpenCode TUI, wired to host Ollama
uv run danno --apply sandbox start --target ./my-project \
    --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY"

uv run danno --apply sandbox shell   --target ./my-project   # bash in the VM
uv run danno --apply sandbox stop    --target ./my-project
uv run danno --apply sandbox rebuild --target ./my-project   # recycle from scratch
```

OpenCode **only ever runs inside the sandbox** — never on your host.

#### Other agents (`--agent`)

`docker sandbox` ships prebuilt agents (`opencode`, `claude`, …). Pass `--agent`
to run a different one; non-default agents get their **own** sandbox
(`danno-<dir>-<agent>`) so they coexist with the opencode sandbox.

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

[[tools]]                         # tool catalog; each installs to sandbox or project
name       = "ados"
source     = "https://github.com/juliusz-cwiakalski/agentic-delivery-os"
install_to = "sandbox"
```

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
  the changelog, then tagging `vX.Y.Z` — full steps in
  [`.specify/memory/parts/shared.md`](.specify/memory/parts/shared.md).

## Where the docs live

- [`docs/ux-requirements.md`](docs/ux-requirements.md) — the reconciled command
  surface, network model, and `danno.toml` schema (the design-of-record).
- [`.specify/memory/constitution.md`](.specify/memory/constitution.md) — the
  authoritative development practices, with per-work-type detail in
  [`.specify/memory/parts/`](.specify/memory/parts/).

## License

MIT — see [LICENSE](LICENSE).
