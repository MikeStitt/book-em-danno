# book-em-danno

`danno` CLI reads a single `danno.toml` then declaratively provisions 
an [OpenCode](https://opencode.ai) or [Claude Code](https://github.com/anthropics/claude-code) 
agentic coding tool **inside a Docker Sandbox**. The `--target` folder defaults to `.`
which is the `cwd` for the sandboxed coding tool. The sandboxed coding tool shares the `--target`
folder, and a local host [ollama](https://ollama.com/) server, accesses the **internet**,
but is sandboxed from the rest of the host and the host **intranet**.

From the sandbox, `claude` uses its normal cast of AI Reasoning models;
while `danno` uses `danno.toml` to configure `opencode` agents to use local or 
cloud AI Reasoning models.

From one file, `danno` writes the OpenCode config, pulls the local models, 
installs a catalog of agentic tools (including [ADOS](https://github.com/juliusz-cwiakalski/agentic-delivery-os)),
and creates a Docker Desktop microVM sandbox wired to the host `ollama`.

Everything is **transparent and non-destructive**: by default `danno` *advises* —
it prints the exact copy-paste commands it would run, without executing them.
Pass `--apply` (a per-command flag, e.g. `danno install --apply`) to execute.

## Install and Tryout

- Install [uv](https://docs.astral.sh/uv/)
- Install [ollama](https://ollama.com/) command line
- Install [Docker Desktop](https://www.docker.com/products/docker-desktop/)

```bash
mkdir tryout
cd tryout
git clone https://github.com/MikeStitt/book-em-danno
cd book-em-danno
uv sync            # install danno + its locked deps (in-project)
uv run danno --help
uv run danno --version

# Install danno as a global tool.

uv tool install . --reinstall  # then `danno` is on PATH
danno --help                   # also works: uv tool run danno --help
danno --version

cd ..
mkdir example-project
cd example-project
git init
curl -L -o danno.toml https://raw.githubusercontent.com/MikeStitt/book-em-danno/refs/heads/main/danno.toml.example

docker desktop start

# From a different terminal window, start ollama  command line.
# Please ensure only one ollama is running.
#
# An example command: 
# OLLAMA_HOST=0.0.0.0:11434 OLLAMA_KEEP_ALIVE=30m OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve

danno doctor

# Perhaps set cloud API keys
export ANTHROPIC_API_KEY=sk-ant-api03-YOUR-KEY
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-YOUR-KEY
export NVIDIA_API_KEY=nvapi-YOUR-KEY

# Perhaps preview what danno will provision:
danno install

# actually do it: write config, pull models, create + wire the sandbox
danno install --apply

touch I-MADE-THIS-FILE

ls -Flag

danno sandbox start
```

Perhaps type `hi` to talk to your AI agent.

Perhaps type `!ls -Flag` to show what the agent can see.

`/exit` to exit opencode.

Perhaps try with API keys:

```bash
danno sandbox start --env ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY --env CLAUDE_CODE_OAUTH_TOKEN=$CLAUDE_CODE_OAUTH_TOKEN --env NVIDIA_API_KEY=$NVIDIA_API_KEY
```

## More detailed Getting Started

### 1. Preflight

```bash
danno doctor
```

A read-only PASS/FAIL/WARN checklist with copy-paste fixes: Python, git, the
Docker daemon, the `docker sandbox` subcommand, Ollama (installed, reachable, a
model pulled), and a WARN if Ollama is bound to loopback only (unreachable from
the sandbox VM).

### 2. Preview, then provision

```bash
# default: write the config we own, PRINT the host/Docker commands to run yourself
danno install --target ./my-project

# actually do it: write config, pull models, create + wire the sandbox
danno install --apply --target ./my-project
```

`install` runs the full happy path in order — validate `danno.toml` → write
`.opencode/opencode.jsonc` → ensure Ollama models → install tools → create the
sandbox — then prints the launch command. It **stops before** launching the TUI.
Re-running is idempotent: an unchanged config is a no-op; a changed config shows a
diff and needs `--apply`.

### 3. Launch and operate the sandbox

The confusion-resistant flow is to `cd` into the project and omit `--target`: it
defaults to `.`, so danno derives the same sandbox name every time.

`start` and `shell` are interactive and behave identically except that `shell`
drops you at a bash prompt instead of launching the agent: launching is their
purpose, so they run without `--apply`, and `--apply` additionally provisions. (On
an unprovisioned sandbox, `start`/`shell` fail loud — provision first with `danno
install --apply` or `danno sandbox start --apply`.) The management commands keep the
advise/`--apply` split.

```bash
cd ./my-project
# launch the in-container OpenCode TUI, wired to host Ollama
danno sandbox start
danno sandbox shell               # bash in the VM
danno sandbox stop --apply
danno sandbox rebuild --apply     # recycle from scratch (agent home survives)
danno sandbox ls                  # which sandbox maps to which project?
```

`--target ./my-project` works too. The sandbox name is `danno-<parent>-<dir>` (the
parent dir is included so same-basename checkouts and worktrees never collide), and
`sandbox ls` reads `~/.danno/sandboxes.json` to print each `name → target` plus live
status. OpenCode **only ever runs inside the sandbox** — never on your host.

#### Forwarding flags to the agent (`--`)

Anything after `--` on `sandbox start` is passed **verbatim** to the agent binary,
so you can use the agent's own flags — for example resuming a prior Claude session
(its history lives in the persistent agent home, so the session is still on disk):

```bash
danno sandbox start --agent claude -- --resume <session-id>
danno sandbox start --agent claude -- --continue   # most recent session
```

danno's own options (`--target`, `--agent`, `--apply`, …) stay before the `--` and
are not forwarded.

#### Capturing model wire traffic (`--capture`)

`--capture` records the request **and** response between the sandboxed agent and its
model backends — useful for seeing exactly what is sent (e.g. that a local model is
being used for an auxiliary call). danno interposes a small recording proxy in front
of each *redirectable* backend by rewriting its `base_url`, then writes one JSONL file
per backend with auth-header values redacted.

```bash
danno sandbox start --capture --apply   # ./.danno/captures/<ts>/<backend>.jsonl
danno sandbox start --capture --apply --agent claurst   # claurst<->Ollama too
danno validate --capture --only <model> # <out>/captures/<backend>.jsonl
```

For `--agent claurst` the lever is different: claurst ignores both `opencode.jsonc`
and the egress proxy, so danno points its in-VM Ollama relay at the same recording
proxy instead. Capture therefore covers claurst's local-Ollama traffic too (the JSONL
shows its system prompt, tool definitions, and the model's reply). Because the proxy
buffers each response before replay, a *captured* interactive claurst session loses
live token-streaming — fine for a diagnostic run, and only while `--capture` is set.

To debug the relay itself (e.g. a session that appears to hang), set
`--env DANNO_RELAY_LOG=/tmp/danno-relay.log`: the in-VM relay then writes a flushed,
per-connection trace of both ends (`CONN open` / `REQ` / `-> upstream` / `<- upstream`
/ `RESP done` / `CONN close`) so the last line written pinpoints exactly where a turn
stalled. Off by default; read it with `docker sandbox exec <name> tail -f /tmp/danno-relay.log`.

**What gets captured depends on whether danno controls the endpoint URL.** A model
reaches danno's proxy only when its traffic flows through a `base_url` danno wrote.
That splits the cases into three:

| Where the model lives | Example | Captured? | Why |
| --- | --- | --- | --- |
| A backend you define (Ollama, or any `openai`-compatible like NVIDIA NIM) | `[backends.danno-nvidia] base_url = …` | **Yes** | danno owns the `base_url`, so it can point it at the proxy (HTTPS is re-originated; the auth header is forwarded upstream and redacted in the capture). |
| A built-in Anthropic model used *inside* opencode | `[agents] pm = "anthropic/claude-sonnet-4-6"` | **No** | a raw OpenCode ref has no danno `base_url`; opencode calls `api.anthropic.com` directly, so there is nothing to redirect. |
| Claude Code itself as the agent | `sandbox start --agent claude`, `validate --baseline` | **No** | a different tool on a fixed Anthropic endpoint; redirecting it would mean injecting `ANTHROPIC_BASE_URL` (a documented follow-on). |

In short: capture covers **anything whose endpoint danno configures** (Ollama,
NVIDIA/openai). It does **not** capture Anthropic-served Claude in either form —
opencode's `anthropic/*` models *or* the `claude` agent — because both use fixed
Anthropic endpoints danno doesn't currently redirect. A captured run that touches
either **warns loudly** naming exactly what it skipped.

`sandbox start`/`shell` need `--apply` (the per-run proxy ports must be opened in the
sandbox egress) and restore your `opencode.jsonc` afterward.

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
the knob to `CLAUDE_CONFIG_DIR` for Claude and `XDG_CONFIG_HOME` for opencode
(opencode's sqlite session store stays VM-local — the virtiofs mount can't run its
WAL journal — so sessions reset on rebuild). See
[Sandboxed agents: repo, agent-home, auth](#sandboxed-agents-repo-agent-home-auth)
for the full model.

#### Other agents (`--agent`)

`docker sandbox` ships prebuilt agents (`opencode`, `claude`, …). Pass `--agent`
to run a different one; non-default agents get their **own** sandbox
(`danno-<parent>-<dir>-<agent>`) so they coexist with the opencode sandbox.

```bash
danno sandbox start --apply --target ./my-project --agent claude
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

**claurst (local-only)** — `--agent claurst` runs [claurst](https://github.com/Kuberwastaken/claurst),
a pure-Rust Claude-Code clone, on **local Ollama only** (no cloud, no auth). claurst
isn't a prebuilt image, so danno hosts it in the `shell` sandbox and curl-installs the
binary on first provision. Pick the model with `-m <name>` (a danno.toml `[models]`
entry); cloud/non-Ollama models are **rejected loudly** (claurst's client can't reach
them through the sandbox proxy).

```bash
danno sandbox start --apply --agent claurst              # claurst's default model
danno sandbox start --apply --agent claurst -m gemma4    # a local [models] entry
```

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

# Cloud models need NO backend or [models] entry: OpenCode knows them via its
# built-in catalog and launch-time auth. Reference one inline in [agents] as a raw
# OpenCode ref (any value containing "/"); the API key stays in the env.

[backends.nvidia]                 # any OpenAI-compatible endpoint (NVIDIA NIM, vLLM, OpenAI)
kind        = "openai"
base_url    = "https://integrate.api.nvidia.com/v1"
api_key_env = "NVIDIA_API_KEY"    # emitted as {env:NVIDIA_API_KEY}; the secret is never
                                  #   written here. At launch danno auto-injects it if
                                  #   exported, else `--env NVIDIA_API_KEY=…`, else fails loud.

[models.gemma4]
backend          = "ollama"
tag              = "gemma4:26b"   # local models MUST be tool-capable (gemma3:1b is NOT)
reasoning_effort = "none"         # disable the thinking trace; see knobs below

[models.nemotron]
backend = "nvidia"
tag     = "nvidia/nemotron-3-ultra-550b-a55b"   # the model id the endpoint expects

[agents]                          # agent -> model. A value WITH "/" is a raw OpenCode
                                  #   ref (built-in cloud model, no backend needed); a
                                  #   value WITHOUT "/" names a [models] entry above.
pm        = "anthropic/claude-sonnet-4-6"   # raw ref → high-stakes cloud, passed through
architect = "nemotron"
runner    = "gemma4"
committer = "gemma4"

[[tools]]                         # imperative tools (see "Two install lanes" below)
name       = "ados"
source     = "https://github.com/juliusz-cwiakalski/agentic-delivery-os"
install_to = "sandbox"
```

#### Rich agents

The string form (`agent = "model"`) sets only the model. For more, use the table
form `[agents.<name>]` — `model` (same `/` rule) plus OpenCode agent fields
(`mode`, `prompt`, `temperature`, `permission`, …) emitted verbatim into the
generated `agent.<name>` block. Two uses: route a built-in subagent to a local
model (`[agents.explore]` → `model = "gemma4"`), or fully define a danno-owned
agent in JSON. danno **never** writes `.opencode/agent[s]/*.md`; where a markdown
agent def already sets a field, OpenCode's **markdown wins** over the generated
JSON, so `danno install` warns loud at that collision rather than emitting a value
that will be silently ignored.

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

- **danno owns only a marked region.** Its keys live between
  `// >>> danno:managed …` and `// <<< danno:managed <<<` markers. `danno install`
  **merges** (not overwrites): it rewrites only that region and **preserves everything
  outside it** — your extra top-level keys and inline comments survive across re-runs.
  Edits *inside* the markers are reasserted from `danno.toml`.
- **Only `danno install` touches it.** First run writes it automatically. On a re-run,
  danno compares the merged result to what's on disk: if they differ it prints a
  unified diff and **refuses to write unless you pass `--apply`**. `sandbox`/`doctor`/
  etc. never touch the file. (Adopting a *pre-existing, unmarked* `opencode.jsonc` is a
  one-time wholesale write that installs the markers — review the diff first; every run
  after that is an in-place region merge.)
- **Agent models can land in `.md` instead.** If an agent is defined by an
  `.opencode/agent[s]/<name>.md` (markdown wins over the JSON), danno writes that
  agent's `model` into a danno-managed region of the md's **frontmatter** — never its
  body or behavior fields. Same marker discipline; same merge.
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

**End to end:** `danno install` previews the `"plugin"` array and any
in-container `docker sandbox exec … bash -lc …` setup line; `danno install --apply`
writes the config and runs the setup step post-create; then `danno sandbox start`
launches OpenCode, which installs the plugins in-sandbox on first run. A `package`
with no `config`/`setup` is the minimum — `config` and `setup` are both optional.

## Validate which models actually work (`danno validate`)

Declaring a model in `danno.toml` doesn't mean it can *do the job* — many local
models can't tool-call, stall instead of acting, or pass a chat but fail a real
edit. `danno validate` sweeps every model your `danno.toml` declares through a
tiered battery in a disposable sandbox and reports which ones converge.

```bash
cd ./my-project
danno validate --dry-run                 # preview the plan (models, tiers, sandboxes)
danno validate                           # run it — provisions, sweeps, writes the report
danno validate --only gemma4 --max-level 1   # just one model, liveness + tool/bash
danno validate --baseline --baseline-model opus   # add a Claude Code reference row
danno validate --agent claurst               # sweep the models via claurst, not opencode
```

`--agent` picks the **agent-under-test** that drives the sweep: `opencode` (the
default) or `claurst` (the Rust Claude-Code clone, benchmarked on local Ollama
models). claurst also runs as an **interactive** coding tool via `danno sandbox start
--agent claurst` (see the [`--agent`](#other-agents---agent) section above and
[`.docs/user-experience-elephant.md`](.docs/user-experience-elephant.md) §5).

It **runs immediately** (like `sandbox start`, no `--apply`) and is
**non-destructive**: the battery runs in a throwaway, validator-owned sandbox
seeded from a *copy* of your `danno.toml` — your project, your real sandbox, and
your `danno.toml` are never touched. The tiers short-circuit (a model that fails
liveness never wastes a run on the dev task):

- **L0 — liveness:** a scripted greet → act → nudge; catches the *promised-but-
  didn't-act* stall (says it will act, makes no tool call).
- **L1 — tool/bash:** a deterministic file task (count lines → write the number);
  graded by an objective file check.
- **L2 — software-dev:** implement a function against a **hidden** test suite run
  *in* the sandbox (exit 0 = pass).

Each run writes, under `.danno-validator/<timestamp>/` (gitignored):

- `index.md` + per-config pages — a MyST report: results matrix, per-tier
  transcripts, failure taxonomy;
- `menu.danno.toml` — an annotated **"menu"** config: every `[models.*]` block
  tagged with its `[L0 · L1 · L2]` verdict and `[agents]` rendered as a
  comment/uncomment menu, so you assemble a working config by editing assignments;
- `results.json` — the machine-readable run record (CI/`--strict`, dashboards).

`--baseline` needs a Claude token (`CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`)
and fails loud up front if it's missing. See
[`.docs/ux-danno-validate-cli.md`](.docs/ux-danno-validate-cli.md) for the full
command surface and [`.docs/plan-danno-validator.md`](.docs/plan-danno-validator.md)
for the harness design.

### Benchmark suites across models (`danno bench`)

Where `validate` runs danno's own tiered battery, `danno bench` runs **established
coding-benchmark suites** — an Aider Polyglot subset and a SWE-bench Verified subset
— across every model your `danno.toml` declares, against your chosen
agent-under-test. Suites and the exact instances come from a `benchmarks.toml` (next
to `danno.toml` by default, or `--benchmarks <path>`): enable `[aider_polyglot]`
and/or `[swebench]` and list the exercise/instance ids under each `select`.

```bash
danno bench --dry-run                 # preview the suites × models plan
danno bench                           # run the enabled suites across danno.toml's models
danno bench --only gemma4             # restrict the matrix to one model
danno bench --agent claurst           # benchmark claurst (local Ollama) instead of opencode
```

It provisions disposable, validator-owned sandboxes over a throwaway workspace, runs
each enabled suite for every model variant, writes `bench.json` + a summary under
`.danno-bench/<timestamp>/`, then tears the sandboxes down — your project is never
modified. These run real benchmark *content* through danno's own execution model, not
the official Docker-per-task harness, so the pass counts are **not** official
benchmark scores.

### Benchmark whole configs (`danno benchmark`)

`danno benchmark` (distinct from `danno bench` above) sweeps whole **configs** for
editing performance — to A/B different prompts, permissions, or model assignments.
Each subdirectory of the configs dir is a candidate holding its own `.opencode/` tree
(opencode.jsonc + agent `.md`); danno applies each into the throwaway,
validator-owned workspace and runs the *same* tiered battery as `validate` (plus the
optional Claude `--baseline`), then writes a comparison report + `results.json` under
`.danno-benchmark/<timestamp>/`.

```bash
danno benchmark ./candidate-configs --dry-run        # preview which configs run
danno benchmark ./candidate-configs --baseline       # run + a Claude reference row
danno benchmark ./candidate-configs --judge          # add L2 dev-quality grading
danno benchmark ./candidate-configs --agent claurst  # drive the candidates with claurst
```

Each candidate carries its own model (in its opencode.jsonc), so no `-m` override
is applied. Your project and real `danno.toml` are never touched — `danno.toml` is
read only for sandbox/env setup.

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
`[sandbox]`. See [`docs/danno.workspace.toml-explanation.md`](docs/danno.workspace.toml-explanation.md)
for technical details (implementation, path resolution, limitations). Inheritance covers *nested* layouts; sibling checkouts use `per-repo` or
`group:` instead.

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
| ② Relocated by | `CLAUDE_CONFIG_DIR` | `XDG_CONFIG_HOME` only — the data dir / sqlite stays VM-local because virtiofs can't run WAL, so opencode sessions reset on rebuild |
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
| Start fresh but keep history + rules | `danno sandbox rebuild --apply` |

## Development

- **Gate:** `ninja check` = `ruff check` + `ruff format --check` + `mypy` +
  `pytest` (fast suite). Run the live tests with `uv run pytest -m slow` (they skip
  cleanly when Docker/Ollama are down).
- **Setup:**
  ```bash
  uv sync --locked --dev        # install deps into .venv
  sudo apt-get install ninja-build  # Linux (macOS: brew install ninja)
  ```
  All tools are then available via `ninja check` or `uv run ninja check`.
- **Layout:** `src/book_em_danno/` — `config/` (schema, loader, generator),
  `core/exec.py` (the advise-by-default `Runner`), `commands/` (doctor, ollama,
  sandbox, tools, install), `cli.py`.
- **Releasing:** automated and bot-driven — you never bump the version, write the
  changelog, push a tag, or run any command. You click two buttons in GitHub's web
  UI: on the **Actions** tab, start the `release-prepare` workflow; then **Merge**
  the `chore(release): vX.Y.Z` PR it opens for you. The workflows do everything
  else. Full process, prerequisites, and caveats:
  [`plans/releasing.md`](plans/releasing.md).

## Where the docs live

- [`docs/ux-requirements.md`](docs/ux-requirements.md) — the reconciled command
  surface, network model, and `danno.toml` schema (the design-of-record).
- [`.docs/user-experience-elephant.md`](.docs/user-experience-elephant.md) — the
  end-to-end user story for the three coding-tool use cases (opencode + ADOS, Claude
  Code, and the proposed claurst-on-local-Ollama path).
- [`.docs/ux-danno-validate-cli.md`](.docs/ux-danno-validate-cli.md) — the
  `danno validate` command surface, status reporting, and `results.json` schema;
  [`.docs/plan-danno-validator.md`](.docs/plan-danno-validator.md) — the validator
  harness design.
- [`docs/danno.workspace.toml-explanation.md`](docs/danno.workspace.toml-explanation.md) —
  detailed technical explanation of workspace configuration inheritance (for developers).
- [`.specify/memory/constitution.md`](.specify/memory/constitution.md) — the
  authoritative development practices, with per-work-type detail in
  [`.specify/memory/parts/`](.specify/memory/parts/).

## License

MIT — see [LICENSE](LICENSE).
