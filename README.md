# claude-setup-agentic-delivery-os

Bash-first tooling that installs and adjusts
[Agentic Delivery OS (ADOS)](https://github.com/juliusz-cwiakalski/agentic-delivery-os)
onto a project and wires it to a **hybrid model runtime**: local
[Ollama](https://ollama.com)-served **Gemma** for the high-volume agents, **cloud**
models for the high-stakes agents, with the ADOS/OpenCode agents running inside an
isolated **Docker (Linux microVM) sandbox**. Tuned for a local machine like a
MacBook Pro — Gemma inference runs natively on the Mac GPU; only command/build
execution runs in the VM.

Full narrative walkthrough:
[`.docs/user-experience-elephant.md`](.docs/user-experience-elephant.md).
Development rules: [`.specify/memory/constitution.md`](.specify/memory/constitution.md).

## What it puts on your Mac (host)

| Tool | Role | Often already present? |
|---|---|---|
| Homebrew | installs the rest | usually |
| `bash` ≥5 (brew) | the toolkit needs ≥4; macOS ships 3.2 | no |
| Docker Desktop (+ `docker sandbox`) | the isolated Linux microVM | sometimes |
| Ollama + a Gemma model | local inference on the Mac GPU | sometimes |
| OpenCode | the agent runtime ADOS runs in | sometimes |
| ADOS (`~/.ados`) | the delivery framework | no |

Inference (Gemma) runs natively on the Mac; only the agents' command/build
execution runs in the Linux VM. Run `tools/ados-ollama-doctor` to see what's
missing.

## Quickstart: put `elephant` under ADOS-on-Ollama

The paths below set the toolkit location; the Anthropic key is only needed for the
cloud and hybrid paths (not the fully-local one):

```bash
TOOLKIT=~/projects/claude-setup-agentic-delivery-os   # this repo
export ANTHROPIC_API_KEY=sk-ant-...                   # cloud + hybrid paths only
```

### All-cloud — Anthropic Opus 4.8 (no local model)

Every agent runs on `anthropic/claude-opus-4-8`; no Ollama needed. Agents still
execute isolated in the Docker sandbox.

```bash
git clone https://github.com/MikeStitt/elephant.git
cd elephant

# install ADOS, then write an all-cloud Opus config (no local agents)
"$TOOLKIT/tools/ados-ollama" install   --target .
"$TOOLKIT/tools/ados-ollama" configure --target . \
  --cloud-only --cloud-model anthropic/claude-opus-4-8
git add -A && git commit -m "chore: ADOS config (cloud-only, Opus 4.8)"

# launch OpenCode in the sandbox (inject key + allow Anthropic through the proxy)
"$TOOLKIT/tools/ados-sandbox" --target . \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --allow-host api.anthropic.com
```

Use `install` + `configure` (not `all`) here — `all` runs the Ollama pull that a
cloud-only setup doesn't need. (`doctor` will flag missing Ollama/Gemma; ignore
that in cloud-only mode.)

### Hybrid — local `gemma4:26b-mlx` + Opus 4.8 cloud

Local high-volume agents (`committer`, `runner`, `external-researcher`, `image-*`)
run on **`gemma4:26b-mlx`** on your Mac's GPU; high-stakes agents run on Opus 4.8.
`gemma4:26b-mlx` is the 26B Mixture-of-Experts Gemma 4 (~4B active) in Apple's MLX
format — fast on Apple Silicon, ~17 GB, text-only (fine for coding agents).

```bash
# host: make Ollama reachable from the sandbox, then pull + PROVE tool-calling
export OLLAMA_HOST=0.0.0.0:11434          # add to your profile; restart Ollama
"$TOOLKIT/scripts/setup-ollama.sh" --model gemma4:26b-mlx --verify-tools
#   want "Tool-calling works on gemma4:26b-mlx"; if it WARNs, use gemma4:26b (GGUF)

git clone https://github.com/MikeStitt/elephant.git
cd elephant

# install + pull + write the hybrid config (preview with --dry-run first)
"$TOOLKIT/tools/ados-ollama" all --target . \
  --model gemma4:26b-mlx --cloud-model anthropic/claude-opus-4-8 --dry-run
"$TOOLKIT/tools/ados-ollama" all --target . \
  --model gemma4:26b-mlx --cloud-model anthropic/claude-opus-4-8
git add -A && git commit -m "chore: ADOS config (hybrid: gemma4:26b-mlx + Opus 4.8)"

# launch (local half hits host Ollama via host.docker.internal; cloud half Anthropic)
"$TOOLKIT/tools/ados-sandbox" --target . \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --allow-host api.anthropic.com
```

### Fully local — `gemma4:26b-mlx` only (no cloud)

Every agent — including the high-stakes ones — runs on `gemma4:26b-mlx` via
Ollama. No API key and no cloud egress: fully private/offline. The tradeoff is
that the hard agents (`architect`, `reviewer`, …) now run on Gemma instead of
Opus, so expect lower quality on those.

```bash
# host: make Ollama reachable from the sandbox, then pull + PROVE tool-calling
export OLLAMA_HOST=0.0.0.0:11434          # add to your profile; restart Ollama
"$TOOLKIT/scripts/setup-ollama.sh" --model gemma4:26b-mlx --verify-tools

git clone https://github.com/MikeStitt/elephant.git
cd elephant

# install + pull + write an all-local config (every agent on the Gemma model)
"$TOOLKIT/tools/ados-ollama" all --target . --model gemma4:26b-mlx --local-only
git add -A && git commit -m "chore: ADOS config (fully local, gemma4:26b-mlx)"

# launch — no cloud key or extra --allow-host needed (host.docker.internal is allowed)
"$TOOLKIT/tools/ados-sandbox" --target .
```

### Then, inside OpenCode

Drive ADOS as usual — autopilot `@pm deliver change GH-1`, or step by step
(`/plan-change` → `/write-spec` → … → `/pr`). Everything the agents run executes
in the sandbox VM.

### Re-launching the sandbox

`ados-sandbox` creates the sandbox the first time and configures its network. After
that, Docker reports it already exists — re-open it directly with:

```bash
docker sandbox run ados-<dirname>      # e.g. docker sandbox run ados-elephant
```

This reuses the configured proxy and the project's `.opencode/opencode.jsonc`. For
the **all-cloud / hybrid** paths, re-launch via `ados-sandbox` instead (it injects
your API key), or run `opencode auth login` once inside the sandbox; the
**fully-local** path needs no key. The host must have `OLLAMA_HOST=0.0.0.0:11434`
set (and Ollama restarted) so the VM can reach it.

### Getting work out / copy-paste

The project is **mounted** into the sandbox, so files the agents create or edit
appear directly in your local clone — commit them from the host as usual; there's
nothing to copy out. For terminal text, use your terminal's normal selection (in
iTerm2 / Terminal.app, hold **Option** while dragging to bypass the TUI's mouse
capture).

## Commands

| Command | Role |
|---|---|
| `tools/ados-ollama` | orchestrator: doctor / install / setup / configure / sandbox / all |
| `tools/ados-ollama-doctor` | read-only preflight (host deps) |
| `scripts/setup-ollama.sh` | ensure Ollama + pull/verify a Gemma model |
| `scripts/install-ados.sh` | ADOS install + project-local agent/command defs + provenance |
| `tools/gen-opencode-config` | write the hybrid `.opencode/opencode.jsonc` |
| `tools/ados-sandbox` | run OpenCode in an isolated Docker sandbox |
| `tools/ados-sandbox-macos` | experimental Seatbelt write-confinement (macOS-native work) |

Every mutating script supports `-n`/`--dry-run` and is idempotent. Use `-h` on any
of them for the full flag list.

## Parameters & configuration you edit

**Flags (most common):**

- **Model** — `--model <tag>` (orchestrator/setup; sets both the local model and
  the configured `ollama/<tag>`), `--cloud-model <provider/model>` (cloud half,
  default `anthropic/claude-sonnet-4-6`), `--num-ctx <n>` (local context, default
  `32000` — keep high or local tool calls fail).
- **Target** — `--target <dir>` (the project; default `.`).
- **Sandbox** (`tools/ados-sandbox`) — `--ollama-url <url>` (default
  `http://host.docker.internal:11434/v1`), `--allow-host <host>` (repeatable;
  permit a domain through the egress proxy — add your cloud provider's host),
  `--env KEY=VAL` / `--env-file <f>` (inject keys), `--name <n>`, `--no-launch`,
  `--use-run`. Forward these through the orchestrator after `--`, e.g.
  `ados-ollama sandbox --target . -- --env ANTHROPIC_API_KEY=…`.

**Environment variables:**

- `OLLAMA_HOST=0.0.0.0:11434` — host Ollama bind so the VM can reach it (required).
- `OLLAMA_BASE_URL` — read by the generated config; the sandbox sets it to the
  host gateway, on the host export `http://localhost:11434/v1`.
- `ANTHROPIC_API_KEY` (or your provider's key) and a GitHub token — injected into
  the sandbox via `--env`/`--env-file`; never written to committed files.

**Files you may hand-edit:**

- **The hybrid agent map** — the `CLOUD_AGENTS` / `LOCAL_AGENTS` arrays at the top
  of `tools/gen-opencode-config`. Move an agent between local and cloud here (one
  line), then re-run `configure`.
- **`<project>/.opencode/opencode.jsonc`** — the generated config. Hand-edits are
  **preserved**: a later `configure` won't overwrite a differing file unless you
  pass `--force`. Keys stay `{env:…}` (no secrets in the file).
- **`<project>/.ai/agent/pm-instructions.md`** — ADOS tracker/PR config. Not
  written by this toolkit; create it via ADOS `/bootstrap` and edit per project.
- **Docker Desktop ▸ Resources** — CPU/RAM given to the sandbox VM (there is no
  per-sandbox flag). Size it for heavy builds while leaving room for Gemma + macOS.

## What the tools create & edit

**In the target project** (e.g. `elephant`):

- `scripts/install-ados.sh` →
  `.opencode/agent/*.md`, `.opencode/command/*.md` (project-local copies, so the
  sandbox sees them), `.opencode/ados-provenance.txt` (source + commit), and — via
  ADOS's own `--local` installer — `doc/**` (guides, templates, decision stubs,
  index), `.ai/**` (rules index, agent/local stubs), and an added `.ai/local/`
  entry in the project `.gitignore`. Project-specific files are preserved.
- `tools/gen-opencode-config` → `.opencode/opencode.jsonc` (the hybrid model map).

**On the host:**

- `scripts/setup-ollama.sh` → pulls the model into `~/.ollama` (no project edits).
- `scripts/install-ados.sh --global` (opt-in) → `~/.ados/repo` and agent/command
  defs in `~/.config/opencode`.
- `tools/ados-sandbox` → creates a Docker sandbox VM named `ados-<dir>`, configures
  its network proxy, and writes a 600-perm **temp** env-file (deleted on exit). It
  does not edit project files.
- `tools/ados-sandbox-macos` → writes a **temp** Seatbelt profile (deleted on exit)
  and runs the command confined; nothing persistent.

## Network model (Docker sandbox)

The agents run in a Docker **microVM** — its own kernel, filesystem, and network.
Only the target project is mounted in; the rest of your Mac's filesystem is
invisible to them. Egress is governed by the sandbox proxy, which `ados-sandbox`
configures as **`--policy allow --allow-host localhost:11434`**.

```text
                  ┌──────────────────── your Mac (host) ────────────────────┐
  internet  ◀─allow─▶                                   Ollama :11434 (0.0.0.0)
  cloud API ◀─allow─▶   Docker microVM ── allow ───────▶ (agent dials
                    │     OpenCode + ADOS agents           host.docker.internal,
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

So the agents can explore the internet and use your local Gemma, but cannot reach
the rest of your machine or network.

**Why the allow-rule is `localhost:11434`, not `host.docker.internal`:** the proxy
rewrites `host.docker.internal` → `localhost` before matching the allow-list, so
the rule must name `localhost:11434`. The agent's config baseURL still uses
`http://host.docker.internal:11434/v1` — that's the address it dials.

**Prerequisite:** host Ollama must listen on `0.0.0.0` (`OLLAMA_HOST=0.0.0.0:11434`),
not the default `127.0.0.1`, or the VM can't reach it.

**To lock it down further** (allowlist instead of open internet), set deny-by-default
and name the hosts you trust, then restart the sandbox:

```bash
docker sandbox network proxy ados-<dir> --policy deny \
  --allow-host localhost:11434 --allow-host api.anthropic.com \
  --allow-host registry.npmjs.org
```

## Context window & memory

The toolkit sets the local model's context window with `--num-ctx` (default
`32000`), writing both `num_ctx` (Ollama's window) and `limit.context` (OpenCode's
budget). A fresh ADOS session already consumes ~12–13K of that window for the
system prompt, agent instructions, tool definitions, and project files, so 32K
leaves ~19K for work. `gemma4:26b` supports up to 256K.

Larger contexts cost RAM (the KV cache). Resident memory measured via `ollama ps`
for **`gemma4:26b` (GGUF)** on a 64 GB M3 Max — weights are ≈19 GB:

| `--num-ctx` | resident RAM (model + KV cache) | KV cache over weights |
| --- | --- | --- |
| 32768 (32K) | 20 GB | ~1 GB |
| 65536 (64K) | 21 GB | ~2 GB |
| 131072 (128K) | 22 GB | ~3 GB |
| 262144 (256K) | ~23 GB (extrapolated, +1 GB per doubling) | ~4 GB |

The default **MLX** variant (`gemma4:26b-mlx`) holds ≈16 GB of weights and allocates
the KV cache **lazily** as the context fills, so it starts ~16 GB and grows toward
the same KV-cache sizes above rather than reserving them upfront.

These figures are the model alone. A Docker sandbox VM running at the same time
adds whatever you give it in Docker Desktop ▸ Resources; weights + KV cache + VM
must fit in unified memory.

### Change the context size

```bash
TOOLKIT=~/projects/fab-ri-na-tor/claude-setup-agentic-delivery-os

# 1. Regenerate the project's config at the new size (sets num_ctx + limit.context)
"$TOOLKIT/tools/gen-opencode-config" --target ~/projects/elephant \
  --local-model gemma4:26b-mlx --local-only --num-ctx 131072 --force

# 2. (optional) commit the config change in the project
git -C ~/projects/elephant add -A
git -C ~/projects/elephant commit -m "chore: set context window to 128K"

# 3. Restart so Ollama reloads the model at the new context
docker sandbox run ados-elephant
```

`ollama ps` shows the loaded model's size and context after it restarts.

## Development

```bash
make check     # shellcheck + shfmt -d + bats (the quality gate)
make fmt-fix   # apply shfmt formatting
make test      # bats suites only
make help      # list verbs
```

Standards live in the constitution and its per-work-type parts under
`.specify/memory/parts/`. Live plan + progress:
[`.docs/plan-ados-ollama-tooling.md`](.docs/plan-ados-ollama-tooling.md).

## License

MIT — see [`LICENSE`](LICENSE).
