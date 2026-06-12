# Working with Claude (and opencode) in a danno sandbox

> **Status:** design of record. All three layers now ship — including the durable
> per-key agent home (②). This file is the reference for danno's sandboxed-agent UX.

danno runs your coding agent — **Claude Code** or **opencode** — inside an isolated
Docker Desktop microVM, wired to your local Ollama models and your repo. You get a
real agent on your code without giving it your laptop.

The whole system is **three layers**. Once you see them, everything else follows.

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
lives in a host folder keyed by `agent_home` (below).

---

## Configure

Everything is driven by `danno.toml`. The one new knob is **where the agent home
lives**:

```toml
[sandbox]
# Each sandbox's "agent home" = its global config + chat history + onboarding.
# The value is an IDENTITY KEY: sandboxes whose key resolves to the same value
# share one home; everything else stays separate. Accepted forms:
#   per-project  (default) — its own host folder, keyed on the workspace's full path.
#   per-repo               — one home per git repo, SHARED across that repo's worktrees.
#   shared                 — one host folder shared by ALL danno sandboxes.
#   ephemeral              — inside the VM only; wiped on every rebuild.
#   "group:<name>"         — shared by every toml that names the same group (any location).
#   "<path>"               — an explicit host folder you pick; same path = shared.
agent_home = "per-project"
```

Auth is **never** in `danno.toml` — it stays in your environment:

```bash
# Claude: subscription token (preferred) …
claude setup-token            # run on the HOST; the VM has no browser
export CLAUDE_CODE_OAUTH_TOKEN='…'
# … or API billing
export ANTHROPIC_API_KEY='sk-ant-…'
```

## Use

**The recommended flow is to `cd` into the project and omit `--target`/`--name`** —
`--target` defaults to `.`, so danno recomputes the same sandbox name every time and
you stand *in* the sandbox's directory rather than naming it:

```bash
cd ~/work/acme
danno --apply sandbox start --agent claude   # . is the target; name is derived
danno --apply sandbox shell                  # poke around the VM
danno --apply sandbox rebuild                # fresh VM, keeps layers ① ② ③
```

`--target ~/work/acme` works too if you prefer to name it explicitly. Forgotten which
container belongs to which project? **`danno sandbox ls`** prints every recorded
`name → target` and whether it's currently live (backed by `~/.danno/sandboxes.json`,
which also warns if a name would collide with a different path).

danno launches the agent **in your mounted repo** (the `-w` fix), so it sees
`CLAUDE.md`, edits land in your repo, and history keys to the real project path.

---

## The scenario: one repo, several worktrees

Say you keep a main checkout plus two `git worktree` siblings on different branches:

```
~/work/acme           branch: main
~/work/acme-login     branch: feature/login      (git worktree)
~/work/acme-billing   branch: feature/billing    (git worktree)
```

Each directory is a **separate workspace**, so danno gives each its **own sandbox**
and its **own agent home**:

Sandbox names are `danno-<parent>-<dir>[-<agent>]` — the parent dir is included so
same-basename checkouts (and worktrees) never collide:

| Directory | Sandbox | Agent home (`per-project`) |
|---|---|---|
| `~/work/acme` | `danno-work-acme-claude` | `~/.danno/agent-home/danno-work-acme-claude/` |
| `~/work/acme-login` | `danno-work-acme-login-claude` | `~/.danno/agent-home/danno-work-acme-login-claude/` |
| `~/work/acme-billing` | `danno-work-acme-billing-claude` | `~/.danno/agent-home/danno-work-acme-billing-claude/` |

What you get:

- **Shared instructions, separate minds.** `CLAUDE.md` and `.claude/commands/` are
  committed, so all three branches inherit the same project rules via git. But each
  Claude has its **own chat history and todos**, scoped to its branch. The login
  agent never sees the billing agent's conversation.
- **Run them at once.** Three sandboxes, three agent homes — no two processes write
  the same history file, so parallel work can't corrupt state.
- **Rebuild one freely.** `rebuild` on `acme-login` resets only that VM; its history
  persists on the host, and the other two are untouched.

## Why separate "User Global" is the default (and the best practice)

In normal use, Claude Code keeps one global home (`~/.claude`) shared across every
project on your machine. In danno, each sandbox gets its **own**. That is deliberate:

1. **It matches how you actually work.** A worktree exists to isolate a branch's
   changes; isolating that branch's *agent context* is the same instinct. No
   cross-branch memory bleed, no "why is it talking about billing in the login repo."
2. **Security.** A sandbox can only ever see its own home. A prompt-injection or a
   malicious dependency in one repo can't read another project's history — and can
   never touch your real `~/.claude` (it isn't mounted at all).
3. **Reproducibility.** A sandbox's behavior is a function of its repo + its own home.
   No invisible global state leaking in from unrelated projects.
4. **No corruption.** Parallel agents never contend over one history db.

### Can you make them the same? Yes — opt in.

Set one knob and every danno sandbox shares a single home:

```toml
[sandbox]
agent_home = "shared"     # one ~/.danno/agent-home/shared/ for all sandboxes
```

You'd want this if you treat the agent like a single assistant with one long memory
and one set of preferences/slash-commands across all your work. The trade-offs are
the exact inverse of the four points above: cross-project history is visible to every
sandbox, two sandboxes running at once can corrupt the shared history, and isolation
is weaker. **Recommendation: keep `per-project`; reach for `shared` only when you
specifically want one continuous memory.** (And never choose "share my real
`~/.claude`" — see Security.)

## Sharing a home across a *set* of projects (the middle ground)

Between "every sandbox isolated" and "everything shares one brain" is the common
real case: **a family of related checkouts that should share a home, while staying
private from the rest of your work.** The rule is always the same — *`agent_home` is
an identity key; equal keys share a home* — so you just choose how the key is set.

**Worktrees of one repo → use `per-repo` (zero config).** Every `git worktree` of a
repository shares one git dir (`git rev-parse --git-common-dir`), so danno can key the
home on it automatically:

```toml
[sandbox]
agent_home = "per-repo"   # acme, acme-login, acme-billing all share ONE home; other repos don't
```

`~/work/acme`, `~/work/acme-login`, `~/work/acme-billing` land in a single home keyed
on the shared `.git` — no path to type, no label to invent.

**Any arbitrary set → name a group.** When the projects aren't worktrees of one repo
(or live anywhere on disk), give them a shared label:

```toml
# in each project's danno.toml that should share
[sandbox]
agent_home = "group:acme"   # → ~/.danno/agent-home/groups/acme/
```

Same `group:` name = same home, regardless of location. You pick the name; danno owns
the folder.

**Pick the folder yourself → an explicit path at a midpoint.** Point a set of tomls
at one directory partway up the tree:

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

---

## opencode: same story, different drawers

opencode follows the **same three layers** — the only thing that differs is the
plumbing of the agent-home layer, which danno hides for you.

| | Claude Code | opencode |
|---|---|---|
| ① Repo config | `CLAUDE.md`, `.claude/` (you commit it) | `.opencode/opencode.jsonc` (**danno generates it from `danno.toml`**) |
| ② Agent home | one dir: `~/.claude/` (+`~/.claude.json`) | XDG dirs: `~/.config/opencode/`, `~/.local/share/opencode/` (sessions live in a sqlite `opencode.db`) |
| ② Relocated by | `CLAUDE_CONFIG_DIR` | `XDG_CONFIG_HOME` + `XDG_DATA_HOME` |
| ③ Auth | `CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` in env | local Ollama needs none (baked `baseURL`); cloud providers via env keys |

You set the **same** `agent_home` knob; danno translates it to `CLAUDE_CONFIG_DIR` for
Claude and to `XDG_*` for opencode. So as a user you learn the layers **once** and
both agents behave the same: repo config in git, private history per project (or
shared if you choose), auth in your env.

The one real conceptual difference to know: with Claude you *write* the repo config
(`CLAUDE.md`); with opencode danno *generates* it (`.opencode/opencode.jsonc`) from
`danno.toml`, so you edit `danno.toml`, not the generated file.

---

## Security notes

- danno mounts **only your repo** (and, with `per-project`/`shared`, a dedicated
  agent-home folder). Your real `~/.claude` is **never** mounted — so your global
  credentials, MCP secrets, and every project's history stay off the sandbox.
- Auth is injected as an env token through a `chmod 600` file that's deleted right
  after launch. It never lands on the VM's disk.
- Don't `docker sandbox save` a VM whose agent home holds credentials — that bakes
  secrets into an image.

## Quick reference

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
