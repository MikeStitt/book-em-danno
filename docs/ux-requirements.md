# danno — UX & design-of-record

The reconciled specification for the `danno` CLI as built. Supersedes the earlier
ux-requirements/conversion-plan drafts. The README is the user-facing
getting-started guide; this file is the design reference.

## 1. What danno is

A Python CLI that provisions an OpenCode hybrid local/cloud model runtime in a
Docker sandbox from a single `danno.toml`. It is **transparent** (prints the exact
commands it would run) and **non-destructive/idempotent** (safe to re-run; never
clobbers files we don't own without explicit `--apply`).

## 2. Command surface

Three commands. The distribution is named `danno`; `book-em-danno` is a legacy
script alias for the same entry point.

### `danno install [--target .] [--ados-repo DIR]`

The one provisioning path. Runs in order, each step honoring the two-tier policy:

1. **Validate** `danno.toml` — loud failure (exit 2) on bad input.
2. **Config** (Tier-1, files we own): write `.opencode/opencode.jsonc` from the
   agent→model map. First run auto-writes; a changed file shows a diff and needs
   `--apply`; identical = no-op.
3. **Ollama models** (Tier-2): for each local model tag referenced by an agent,
   advise `ollama pull <tag>` (run with `--apply`).
4. **Tools** (Tier-2): install each catalog entry per `install_to`.
5. **Sandbox** (Tier-2): `docker sandbox create` + egress proxy hole + stop (so
   the policy applies on next start). **Does not launch the TUI.**
6. Print: `ready — launch with: danno sandbox start --target <t>`.

| Step | `danno install` (default) | `danno install --apply` |
| --- | --- | --- |
| config (Tier-1) | write first-run / diff-then-stop if changed | write (overwrite if changed) |
| ollama / tools / sandbox (Tier-2) | print copy-paste commands | execute |

### `danno doctor`

Read-only preflight. PASS/FAIL/WARN with copy-paste fixes: Python ≥ 3.13, git,
Docker daemon up, `docker sandbox` present, Ollama installed/reachable, a model
pulled, and the loopback-bind WARN. Non-zero exit on any required FAIL. **No host
`opencode` check** — OpenCode runs only in the sandbox. Changes nothing.

### `danno sandbox <start|shell|stop|rebuild|update|ls> [--target .] [--name N] [--env K=V] [--env-file F]`

Operate the provisioned sandbox. `start` launches the in-container agent **in the
mounted repo** (`-w <target>`) wired to host Ollama — launching is its purpose, so
it runs without `--apply`; `--apply` additionally provisions (create + egress hole)
first, and on an unprovisioned sandbox without `--apply` it fails loud with the fix.
`shell` opens bash in the VM (also launches by default). `stop`/`rebuild`/`update`
keep the advise/`--apply` split; `ls` is read-only and lists recorded sandboxes
(`name → target`, live status) from `~/.danno/sandboxes.json`.

The default sandbox name is `danno-<parent>-<dir>[-<agent>]` — the parent dir is
included so same-basename checkouts and worktrees never collide. The recommended
flow is `cd <project> && danno sandbox <cmd>` (no `--target`/`--name`): `--target`
defaults to `.`, so the name is recomputed identically each time. The registry warns
loudly if a `--name` would bind to a different target than one already recorded.

**Agent home (`[sandbox] agent_home`):** each sandbox gets a durable host folder
(chat history, settings, onboarding) keyed by `agent_home`, mounted as a second
workspace and relocated per agent (`CLAUDE_CONFIG_DIR` for claude; `XDG_CONFIG_HOME`
for opencode — its sqlite session store stays VM-local because the virtiofs mount
can't run WAL). The mounted config survives `rebuild`; opencode sessions reset on
it. `per-project` (default) keys
on the sandbox name; `per-repo` keys on the shared `.git` common dir (worktrees
share); `shared` is one home for all; `ephemeral` is VM-local (None mounted); a
`group:<name>` or explicit `<path>` lets a chosen set share. A relative path inherited
from a `danno.workspace.toml` resolves against that file's directory. For claude,
onboarding is pre-seeded into `<home>/.claude.json` so the wizard can't mask the env
auth token. Full model: README "Sandboxed agents: repo, agent-home, auth".

### Flags

`--version` is the only top-level flag. The mode/IO flags are **per-command**
(after the subcommand): `--apply` (execute, on every side-effecting command; the
interactive `sandbox start`/`sandbox shell` launch without it and treat `--apply` as
"provision first, then launch"), `--verbose`/`-v`, and `--config <path>`
(danno.toml, `install` only). E.g. `danno install --apply`, `danno sandbox stop
--apply`. There is no `--dry-run` — the default (advise) already prints without
executing.

## 3. Architecture / network model

- **OpenCode runs ONLY in the container — never on the host.** A hard invariant
  for the CLI and the tests. OpenCode is provided by Docker Desktop's prebuilt
  `opencode` sandbox image (not a host dependency); any interaction goes through
  `docker sandbox exec`.
- **Ollama runs natively** (Metal GPU). The sandbox reaches it via
  `OLLAMA_BASE_URL=http://host.docker.internal:11434/v1`; the egress proxy rewrites
  `host.docker.internal → localhost`, so the allow-rule names `localhost:11434`.
  Host Ollama **must** bind `0.0.0.0` (default `127.0.0.1` is unreachable from the
  VM).
- **Egress policy:** `--policy allow --allow-host localhost:11434` → general
  internet allowed, other host services + LAN denied, Ollama allowed through the
  hole. The policy applies only on a fresh VM start (so provisioning stops the VM
  after configuring).
- **No per-sandbox CPU/mem flag** — size the VM in Docker Desktop ▸ Resources.
- **Env injection:** a temp `--env-file` (chmod 600) carries `OLLAMA_BASE_URL` +
  API keys; keys never land in committed config.
- **ADOS defs go project-local:** the sandbox can't see host `~/.config/opencode`,
  so ADOS `.opencode/{agent,command}/*.md` + `opencode.jsonc` live in the mounted
  project even though ADOS is declared `install_to = "sandbox"` (the project IS the
  mount).

Full diagram + allow/deny table: [README "Network model"](../README.md#network-model-docker-sandbox).

## 4. `danno.toml` schema

Pydantic models in `config/schema.py`; loaded with `tomllib` and validated loudly
at the boundary (unknown keys and dangling references fail).

- `[project]` — `target`.
- `[defaults]` — `default_agent`, `profile` (`hybrid` | `cloud-only` | `local-only`).
- `[backends.<name>]` — discriminated by `kind`:
  - `ollama` (IMPLEMENTED): `base_url`, `context_budget`, `output_limit`.
  - `openai` (IMPLEMENTED): any OpenAI-compatible endpoint (NVIDIA NIM, vLLM,
    OpenAI itself). `base_url`, `api_key_env` (named env var, emitted as
    `{env:VAR}` — the secret is never written here), `context_budget`,
    `output_limit`. At launch, `sandbox start` auto-injects `VAR` if it's exported
    in danno's host environment, else accepts `--env VAR=…`, else **fails loud**
    (rather than letting opencode hit the endpoint with no auth header).
  - `cloud` (IMPLEMENTED): `provider`.
  - `llamacpp` (STUBBED): `base_url` — the generator raises a clear
    "not yet implemented" if a used model references it.
- `[models.<name>]` — `backend`, `tag` (ollama/openai/llamacpp) or `id` (cloud).
- `[agents]` — `agent = model-name`.
- `[[tools]]` — `name`, `source`, `install_to` (`sandbox` | `project`).
- `[sandbox]` — `agent_home` (identity key: `per-project` (default) | `per-repo` |
  `shared` | `ephemeral` | `group:<name>` | `<host path>`). Validated at the boundary:
  an unrecognized bare word fails loud. A `danno.workspace.toml` carrying only
  `[sandbox]` may sit at a parent dir; a project with no `[sandbox]` inherits it.

## 5. Out of scope / stubbed

- llama.cpp backend (schema slot exists; generator raises if used).
- No custom Dockerfile — danno extends Docker Desktop's `docker sandbox`.
- Tools with no known local install mechanism (e.g. plannotator) emit an explicit
  advisory/TODO rather than a fabricated installer.
