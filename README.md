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

## Quickstart: put `elephant` under ADOS-on-Ollama (gemma4:26b-mlx)

`gemma4:26b-mlx` is the 26B Mixture-of-Experts Gemma 4 (~4B active) in Apple's MLX
format — fast on Apple Silicon, ~17 GB, text-only (fine for coding agents).

```bash
TOOLKIT=~/projects/claude-setup-agentic-delivery-os   # this repo

# 0) One-time host setup: make Ollama reachable from the sandbox, then pull + verify
#    the model. host Ollama defaults to 127.0.0.1, which the VM cannot reach.
export OLLAMA_HOST=0.0.0.0:11434          # add to your shell profile; restart Ollama
"$TOOLKIT/scripts/setup-ollama.sh" --model gemma4:26b-mlx --verify-tools
#    -> want "Tool-calling works on gemma4:26b-mlx"; if it WARNs, the local agents
#       won't drive tools — try gemma4:26b / gemma4:31b or keep them on cloud.

# 1) Clone the project
git clone https://github.com/MikeStitt/elephant.git
cd elephant

# 2) Install ADOS + write the hybrid config into elephant.
#    Preview first (changes nothing), then apply. Idempotent: safe to re-run.
"$TOOLKIT/tools/ados-ollama" all --target . --model gemma4:26b-mlx --dry-run
"$TOOLKIT/tools/ados-ollama" all --target . --model gemma4:26b-mlx
git add -A && git commit -m "chore: add ADOS-on-Ollama config"

# 3) Launch OpenCode for elephant inside the isolated Docker sandbox.
#    Inject your cloud key and allow its host through the sandbox egress proxy.
"$TOOLKIT/tools/ados-sandbox" --target . \
  --env ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  --allow-host api.anthropic.com
```

Inside OpenCode, drive ADOS as usual — autopilot `@pm deliver change GH-1`, or
step by step (`/plan-change` → `/write-spec` → … → `/pr`). Local agents
(`committer`, `runner`, …) think on Gemma on your Mac's GPU; high-stakes agents
(`architect`, `reviewer`, `coder`) use the cloud; everything executes in the VM.

> `ados-ollama all` already runs `setup` (model pull) and `install`; step 0 above
> is mainly to set `OLLAMA_HOST` and prove tool-calling before you start.

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
