# User experience: coding tools on a project ("elephant")

This is the end-to-end story of taking a brand-new, nearly-empty GitHub repo
("elephant") and putting an **agentic coding tool** on it with `danno` — every
agent running inside an **isolated Docker Desktop microVM** so a runaway build or
command can't touch the rest of your Mac, wired to **Ollama running natively on your
Mac** for local inference and to **cloud model APIs** for the heavy-reasoning work.

`danno` supports more than one coding tool in that sandbox, and this doc walks the
three use cases side by side:

| Use case | Tool | Reasoning models | Status |
| --- | --- | --- | --- |
| **A** | **opencode + [ADOS](https://github.com/juliusz-cwiakalski/agentic-delivery-os)** | hybrid: local Gemma (Ollama) for high-volume agents, cloud for heavy-reasoning agents | ships today |
| **B** | **[Claude Code](https://github.com/anthropics/claude-code)** | Claude's own cloud models (Opus/Sonnet/…) | ships today |
| **C** | **[claurst](https://github.com/Kuberwastaken/claurst)** (a pure-Rust Claude-Code clone) | **local Ollama only** | **proposed** — see [§5](#5-use-case-c--claurst-on-local-ollama-proposed) |

All three share the same skeleton: `doctor` → `install` → `sandbox start`. They
differ only in the agent that comes up at the end and which models it can reach.

> Everything `danno` does is **transparent and non-destructive**: by default it
> *advises* — prints the exact host/Docker commands it would run, changing nothing —
> and executes only under `--apply` (a per-command flag).

## The cast

- **elephant** — your new, largely-empty project on GitHub.
- **`danno`** — the CLI in this repo. From one `danno.toml` it writes the OpenCode
  config, pulls the local models, installs a catalog of agentic tools, and creates +
  wires the Docker sandbox.
- **the coding tool** — opencode, Claude Code, or (proposed) claurst — running
  **inside the sandbox**, never on your host.

## 0. One-time machine setup (only ever the first time)

Install the host prerequisites, then let `danno doctor` tell you what's missing.
Everything is idempotent — already-present tools are skipped.

```bash
# host prerequisites
#   - uv               https://docs.astral.sh/uv/
#   - ollama           https://ollama.com/
#   - Docker Desktop   https://www.docker.com/products/docker-desktop/  (ships `docker sandbox`)

git clone https://github.com/MikeStitt/book-em-danno.git
cd book-em-danno
uv tool install . --reinstall   # puts `danno` on your PATH
danno --version
```

`danno doctor` is read-only and tells you exactly what (if anything) is missing and
how to fix it: Python, git, the Docker daemon, the `docker sandbox` subcommand,
Ollama (installed, reachable, a model pulled), and a WARN if Ollama is bound to
loopback only (unreachable from the sandbox VM).

```bash
# In a separate terminal, run Ollama bound to 0.0.0.0 so the microVM can reach it:
OLLAMA_HOST=0.0.0.0:11434 OLLAMA_KEEP_ALIVE=30m OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve

docker desktop start
danno doctor
```

## 1. Clone elephant and drop in a `danno.toml`

```bash
cd ~/projects
git clone https://github.com/<you>/elephant.git
cd elephant
curl -L -o danno.toml \
  https://raw.githubusercontent.com/MikeStitt/book-em-danno/refs/heads/main/danno.toml.example
```

`danno.toml` is the single source of truth — local + cloud backends, the per-agent
model map, and the tool catalog. Commit it so every clone of elephant gets the
identical setup.

## 2. Optional cloud auth

Use cases A (its cloud agents) and B (Claude Code) reach cloud providers; export the
keys/tokens before launching. Use case C (claurst) is local-only and needs none.

```bash
# Claude Code prefers a Max/Pro subscription token (no per-token billing):
claude setup-token                 # opens a browser; OAuth against your Max/Pro account
export CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...

# …or API billing, and any other OpenAI-compatible provider you reference:
export ANTHROPIC_API_KEY=sk-ant-api03-...
export NVIDIA_API_KEY=nvapi-...
```

`danno` never writes secrets into config — keys stay as `{env:...}` references and
are injected at launch through a `chmod 600` env-file that's deleted right after.

---

## 3. Use case A — opencode + ADOS (hybrid local/cloud)

The original elephant story: put the **Agentic Delivery OS** (19 agents, a 10-phase
lifecycle) on elephant, running in **opencode**, with the cheap/high-volume agents
thinking on **local Gemma** and only the heavy-reasoning agents (`architect`,
`reviewer`, `coder`) on the cloud.

The relevant slice of `danno.toml`:

```toml
[agents]                                       # agent -> model
pm        = "anthropic/claude-sonnet-4-6"      # raw "/" ref → built-in cloud model
architect = "nemotron"                         # a [models] entry on a cloud backend
runner    = "gemma4"                           # local Ollama, tool-capable
committer = "gemma4"

[[tools]]
name       = "ados"
source     = "https://github.com/juliusz-cwiakalski/agentic-delivery-os"
install_to = "sandbox"
```

Preview, then provision, then launch:

```bash
cd ~/projects/elephant
danno install                  # preview: prints every host/Docker command, changes nothing
danno install --apply          # validate toml → write .opencode/opencode.jsonc → pull models
                               #   → install ADOS (project-local) → create + wire the sandbox
danno sandbox start            # boot the microVM and drop into the opencode TUI, @pm ready
```

`install` runs the full happy path in order and **stops before** launching the TUI;
re-running is idempotent (unchanged config = no-op; a changed one shows a diff and
needs `--apply`). From the TUI, drive ADOS as usual — autopilot `@pm deliver change
GH-1`, or step by step `/plan-change` → `/write-spec` → … → `/pr`.

Under the hood: high-volume agents think on **Gemma on your Mac's GPU**; the
heavy-reasoning agents use the cloud; **every command, build, and test runs in the
isolated VM**. danno *generates* elephant's repo config (`.opencode/opencode.jsonc`)
from `danno.toml`, so you edit `danno.toml`, not the generated file.

---

## 4. Use case B — Claude Code (cloud models)

Same sandbox, same repo mount, same network model — but the agent is **Claude Code**
using **its own cast of Anthropic models**. `claude` is one of the prebuilt
`docker sandbox` agent images, so this is a one-flag change: `--agent claude`.

```bash
cd ~/projects/elephant
danno install --apply                          # provision the sandbox (writes opencode cfg too)
danno sandbox start --agent claude             # launch Claude Code in the mounted repo
```

A non-default agent gets its **own** sandbox (`danno-<parent>-<dir>-claude`) so it
coexists with the opencode one. Claude reads `CLAUDE.md` and `.claude/` straight from
your mounted repo (you author those; danno doesn't generate them). Auth comes from
your host env (the token set in §2) and is injected per launch; if neither token is
set, `--agent claude` **fails loud** with the `claude setup-token` hint rather than
launching unauthenticated.

Anything after `--` is forwarded **verbatim** to the `claude` binary, so its own
flags work — e.g. resuming a prior session (its history lives in the persistent agent
home):

```bash
danno sandbox start --agent claude -- --resume <session-id>
danno sandbox start --agent claude -- --continue        # most recent session
```

**Note on local models:** Claude Code talks to fixed Anthropic endpoints; danno does
not currently redirect it to Ollama, so use case B is **cloud-only**. Wanting a
Claude-Code-shaped UX on **local** models is exactly what use case C is for.

---

## 5. Use case C — claurst on local Ollama (proposed)

> **Status: proposed, not yet implemented.** Today claurst is wired into `danno`
> only as a **headless benchmark agent-under-test** (`danno validate --agent
> claurst`, `danno benchmark`), not as an interactive coding tool. This section
> specifies the UX we'd add and the one real gap behind it
> ([§7](#7-what-claurst-as-a-coding-tool-needs-to-ship)).

**The pitch:** [claurst](https://github.com/Kuberwastaken/claurst) is a pure-Rust
Claude-Code clone whose CLI is deliberately Claude-Code-faithful. Run it as a danno
agent and you get a **Claude-Code-shaped coding experience driven entirely by your
local Ollama models** — no cloud, no per-token billing, no provider keys. It's the
local-first counterpart to use case B.

**The intended UX — identical in shape to use case B:**

```bash
cd ~/projects/elephant
danno install --apply                          # provision (and, for claurst, curl-install it)
danno sandbox start --agent claurst            # launch the claurst TUI in the mounted repo
danno sandbox start --agent claurst -m gemma4  # pick which local model it drives
```

- `--agent claurst` gets its **own** sandbox (`danno-<parent>-<dir>-claurst`), so it
  coexists with the opencode and claude ones — same naming rule as every other agent.
- `-m <name>` selects the local model (a `[models]` entry such as `gemma4`); danno
  maps it to claurst's `-m ollama/<tag>` form (e.g. `ollama/gemma4:26b`). The model
  **must be tool-capable** — every coding agent uses tools.
- Auth: **none**. claurst is local-only, so §2 is skipped entirely.
- `--` forwarding works the same way: tokens after `--` go straight to the `claurst`
  binary.

> The `-m` flag is **not** unique to claurst — it's opencode's model-selection form
> (`-m provider/model`), and danno passes the *same* ref to both. Claude Code is the
> odd one out: it uses `--model <alias>` and ignores `-m`.

**What danno.toml does — and does not — configure.** The `-m <name>` above resolves
against danno.toml's `[models]`/`[backends]`, so danno.toml chooses **which** model
claurst drives. That is the *limit* of it: danno does **not** generate a claurst
config file the way it generates `.opencode/opencode.jsonc`. claurst's own settings
live in `~/.claurst/settings.json`, which danno doesn't write — so the `[agents]`
per-agent model map and the Ollama runtime knobs (`context_budget`,
`reasoning_effort`, `output_limit`) **do not reach claurst**. It runs **one top-level
model** picked by a launch flag, exactly like Claude Code in use case B (the
difference: claurst's `-m` value is *derived from* `[models]`, whereas Claude's
`--model` is a fixed flag unrelated to `[agents]`). Making danno.toml *truly*
configure claurst is a separate, larger feature — see §7.

**The one deliberate limitation — local only, and it fails loud.** claurst's Rust
HTTP client ignores `HTTP(S)_PROXY`, and the sandbox blocks direct egress, so claurst
**cannot reach cloud providers** from inside the VM. `--agent claurst` is therefore
restricted to local-Ollama models; pointing it at a cloud model **errors in the open**
(naming the unreachable provider) rather than silently degrading.

## 6. What all three share (the sandbox model)

Whichever agent you launch, the isolation and wiring are identical:

```text
                  ┌──────────────────── your Mac (host) ────────────────────┐
  internet  ◀─allow─▶                                   Ollama :11434 (0.0.0.0)
  cloud API ◀─allow─▶   Docker microVM ── allow ───────▶ (agent dials
                    │     agent + your repo (rw mount)     host.docker.internal,
  your LAN  ──DENY──│        │                             proxy rewrites→localhost)
  other host ports ─DENY     └─ agent home (relocated to a host folder)       │
                    └──────────────────────────────────────────────────────────┘
```

- **Repo mount.** Only elephant is mounted in, at the same path, read-write. The rest
  of your Mac's filesystem is invisible to the agent.
- **Egress policy.** `--policy allow --allow-host localhost:11434`: public internet
  and cloud APIs allowed; the single host hole is Ollama; your LAN and all other host
  ports denied.
- **Agent home.** Chat history/settings live in a host folder keyed by the
  `[sandbox] agent_home` knob (`per-project` default), so they **survive `rebuild`**.
  danno translates that one knob per agent: `CLAUDE_CONFIG_DIR` for Claude,
  `XDG_CONFIG_HOME` for opencode (claurst's config dir is a §7 detail to confirm).
- **Lifecycle.** `danno sandbox shell` (bash in the VM), `… stop --apply`,
  `… rebuild --apply` (recycle the VM; agent home survives), `… ls` (which sandbox
  maps to which project).

The microVM is **disposable**; nothing important lives only inside it — your repo is
on the host, auth is re-injected each launch, and the agent home is a host folder.

## 7. What claurst-as-a-coding-tool needs to ship

Use case C is a small lift because the hard parts already exist in the validator
(`src/danno_validator/claurst.py`, `src/danno_validator/driver.py`). Promoting it to
an interactive `--agent` involves three pieces:

1. **Host claurst in a `shell` sandbox, then curl-install it.** claurst is **not** a
   prebuilt `docker sandbox` agent image, so `--agent claurst` can't pass straight to
   `docker sandbox create` the way `claude` does. The validator already handles this:
   it provisions the `shell` image and drops the release binary into `~/.local/bin`
   via `install_claurst()` (curl-fetched because `npm i -g claurst`'s postinstall
   connects direct to GitHub and the proxy-only sandbox rejects it; it also
   apt-installs the ALSA runtime claurst links). That install step is idempotent and
   ready to reuse — the launch path just needs to special-case `claurst` onto the
   `shell` image and run it post-provision.

2. **Run the Ollama relay as a persistent background process (the real gap).**
   Because claurst's client ignores the proxy, it can only reach `127.0.0.1`. The
   validator stands up a tiny in-VM relay that listens on `127.0.0.1:11434` and
   re-issues each request to host Ollama **through** the squid proxy, then points
   claurst at it with `OLLAMA_HOST=http://127.0.0.1:11434`. The catch: the validator
   launches that relay **inside each headless exec, so it dies with the turn**. An
   interactive TUI session needs the relay **backgrounded for the life of the
   session** (started on launch, reaped on exit) — this is the only genuinely new
   plumbing.

3. **Surface it in the CLI + docs.** Accept `claurst` as an `--agent` value, map
   danno's `-m <name>` to claurst's `-m ollama/<tag>`, reject cloud models loudly,
   and document the local-only scope. Update `--help` and the README in the same
   commit (Documentation Hygiene).

The three pieces above deliberately give claurst **one** model via a launch flag —
the minimum for the local-Ollama use case.

**A separate, larger decision sits on top of them — generating claurst's config from
danno.toml.** Truly *configuring claurst's AI* — a **per-agent** model map (claurst's
`/managed-agents`), reasoning knobs, or non-Ollama providers — would require a **new
generator target** emitting `~/.claurst/settings.json` from danno.toml, parallel to
the `.opencode/opencode.jsonc` generator (and subject to the same marker-delimited,
idempotent, diff-then-`--apply` discipline). That is a real feature in its own right,
not part of the launch path, and is **not** needed to run claurst on a single local
model. Decide it on its own merits before building.

None of this forks claurst or changes the headless benchmark path — the install step
and relay are lifted/extended, not rewritten; `run_sweep`/the oracle stay untouched.

## Why this is better than doing it by hand

- **One idempotent command per tool** instead of a dozen order-dependent steps you
  have to remember and repeat per project.
- **The wiring that's easy to get wrong is done for you** — the Ollama provider, the
  host-gateway URL so the VM can reach your Mac's Ollama, the per-agent local-vs-cloud
  tiers, the egress policy, and (for claurst) the proxy-bypassing relay.
- **Isolation by default** — agents can't touch anything outside the project; a bad
  build or an injected instruction is contained in the VM, and your real `~/.claude`
  is never mounted.
- **Reproducible & version-controlled** — the config lives in elephant's git, so every
  clone and every teammate gets the identical setup.
- **Preflight catches problems early** — `danno doctor` tells you what's missing before
  you're mid-task wondering why an agent can't reach a model.
