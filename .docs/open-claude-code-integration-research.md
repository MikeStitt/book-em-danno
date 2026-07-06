# Adding `open-claude-code` (`occ`) to danno — integration research

> **Status:** research notes feeding an implementation plan. Written 2026-07-02.
> **Question answered:** when we add `open-claude-code` as a danno agent, does it
> follow the **claude-code**, **opencode**, or **claurst** integration pattern?
> **Method:** read occ's own source (`ruvnet/open-claude-code`, cloned, `v2/src/`,
> ESM `.mjs`) *and* mapped danno's three existing patterns from `src/`. All claims
> are file-and-line cited. Companion to
> [`claude-code-clones-research.md`](claude-code-clones-research.md) (which had occ
> mis-scoped — see [§7 Corrections](#7-corrections-to-the-earlier-clone-research)).
>
> **Source caveat:** occ paths are relative to the clone (`v2/src/…`); occ is under
> active development so line numbers may drift — the *mechanisms* are the durable
> findings. danno paths are live as of this repo state.

## 1. Headline answer

**occ follows the `claurst` pattern — as a *lighter-weight* claurst.**

It is structurally a claurst-class agent: no prebuilt sandbox image (installs into
the `shell` VM), **multi-provider local + cloud**, needs its own `stream-json`
driver, first-class in `validate` + `bench`, and auto-excluded from `benchmark`.
But it is **simpler than claurst on two axes** (spike-verified 2026-07-02): no
config generation, and **no fork** — a tiny undici proxy shim replaces it. It
**still needs claurst's in-VM relay** for local Ollama (see §4).

| Axis | claude (baseline) | opencode (deep) | claurst | **→ occ** |
|---|---|---|---|---|
| Prebuilt `docker sandbox create <x>` image | ✅ | ✅ | ❌ installs into `shell` | **claurst** — `npm i -g @ruvnet/open-claude-code` |
| danno generates a config file | ❌ | ✅ `.opencode/opencode.jsonc` + agent-md | ⚠️ own `models.json`+`settings.json` | **near-zero** — model via `-m` |
| Own `stream-json` driver | ✅ `claude_run` | ✅ `opencode_run` | ✅ `claurst_run` | **new `OccTurn`+`occ_run`** |
| Local Ollama | ❌ | ✅ `base_url` | ✅ in-VM relay | ✅ `OPENAI_BASE_URL` (+ gotchas) |
| Cloud | ✅ Anthropic only | ✅ NIM/raw | ✅ NVIDIA NIM | **✅✅ multi-provider — see §5** |
| Swept across `[models]` / in `bench` | ❌ | ✅ | ✅ | **claurst** — swept + benched |
| In `benchmark` (config-tree comparator) | reference row | ✅ only allowed | ❌ rejected (guard) | rejected for free |

It is **not** opencode-style (occ reads no danno-generated config tree) and **not**
merely a claude-style baseline (that throws away occ's on-thesis local-Ollama sweep
*and* understates its multi-provider cloud reach).

## 2. What the `stream-json` driver does (the seam occ must implement)

danno's validator scores every agent with the **same** oracles (L0 liveness, L1
tool-use, L2 hidden tests), but each agent emits a different dialect of streaming
JSON on stdout:

- opencode → `opencode run --format json`
- claude → `claude -p --output-format stream-json --verbose`
- claurst → `claurst -p --output-format stream-json`
- occ → `occ -p --output-format stream-json` (its own 13 event types)

A stream-json driver is the **per-agent adapter** that hides that difference. For
its agent it: (1) builds the argv (`-p` prompt, model flag, skip-permissions,
cwd/session); (2) spawns it in the sandbox, reading stdout line-by-line (one JSON
event per line); (3) parses each line in *that agent's* schema; (4) **normalizes**
those events onto danno's shared `Turn` protocol (`driver.py:188`) — a uniform
object carrying assistant text, tools called + results, token usage, stop/result,
errors; (5) returns a `Turn` the level runners consume agent-agnostically.

So it is a **translation seam: agent-native stream → danno's uniform `Turn`**. The
oracles grade workspace *side effects*, not transcripts — but the driver still must
parse the stream to detect turn completion, capture usage, catch errors, and drive
multi-turn sessions. Adding occ = one new `OccTurn` + `occ_run` in this shape (the
same artifact claude and claurst each already have). occ's headless surface is real
and matches this shape: `occ -p --output-format stream-json` emits one JSON object
per loop event (`index.mjs:181-207`), from a 13-event async generator in
`core/agent-loop.mjs` (`stream_event`, `tool_progress`, `result`, `stop`, …).

## 3. What occ is (from source)

- **Node/ESM, no build, no native deps.** `bin: { occ: src/index.mjs }`
  (`v2/package.json:6-8`); pure `.mjs` run by Node ≥18; deps are `ink`/`react`
  (pure JS, lazy-loaded only for the TUI). Install: `npm i -g
  @ruvnet/open-claude-code`. Trivial to drop into a Linux container.
- **Headless is clean.** `-p` skips Ink entirely (`index.mjs:179-209`); no keyring,
  no startup network, telemetry is an in-memory stub (`telemetry/index.mjs:16-36`).
- **Config chain** (`config/settings.mjs:67-87`): `~/.claude/settings.json` →
  `<cwd>/.claude/settings.json` → `.claude/settings.local.json` → env overrides →
  CLI flags. Reads `.claude/settings.json` (**not** `.claude.json`).
- **Subagents parsed but not dispatched.** `.claude/agents/*.md` YAML-frontmatter
  (incl. `model:`) is parsed (`agents/parser.mjs:87-98`) but only feeds a `/agents`
  list; the actual `Agent` tool routes off hardcoded `subagent_type` prefixes and
  ignores per-agent `model` (`agent.mjs:62-77`). **danno's "write model into agent
  `.md` frontmatter" lever is useless here** — drive model via `-m` instead.

## 4. The proxy/relay hinge — SPIKE RESULTS (2026-07-02, verified)

danno's sandbox egress is a **squid proxy** (auto-injected as
`HTTP_PROXY=HTTPS_PROXY=http://host.docker.internal:3128`, with
`NO_PROXY=localhost,127.0.0.1,::1`) that **blocks direct egress and rejects CONNECT
to the plaintext `:11434` Ollama port** (`driver.py:85-92`; memory
`sandbox-egress-and-process-lifetime`). Node's global `fetch`/undici ignores proxy
env vars. The spike ran occ + raw probes on the host and inside a real `shell`
sandbox (`occ-spike`, VM node **v22.22.1** — so Node-24 `NODE_USE_ENV_PROXY` is out):

| Test | Result |
|---|---|
| **Host** occ → host ollama (no sandbox) | ✅ **OK** — needs `OPENAI_BASE_URL` + `gpt-`prefixed model + dummy `OPENAI_API_KEY` + **`CLAUDE_CODE_STREAMING=0`** |
| In-VM raw `fetch` → ollama, **no proxy fix** | ❌ `ECONNREFUSED` (direct egress blocked) |
| In-VM `npm i undici` (HTTPS `:443` CONNECT) | ✅ OK — CONNECT to `:443` is allowed |
| In-VM undici **shim** (`EnvHttpProxyAgent`) → **cloud** `:443` | ✅ **200 OK** — shim carries the cloud path |
| In-VM undici **shim** → **ollama** `:11434` | ❌ `UND_ERR_SOCKET` — undici only CONNECT-tunnels; squid **rejects CONNECT to :11434** |
| In-VM claurst-style **plain-forward relay** (127.0.0.1:11434, NO_PROXY-direct) → ollama | ✅ **200 OK** |
| **Capstone:** occ in-VM → relay → host ollama | ✅ **`OCC_SANDBOX_OK`** |

**Verdict — the earlier hypothesis was half right.** occ needs **both**:

1. **The undici shim** — `NODE_OPTIONS=--import=<shim.mjs>` running
   `setGlobalDispatcher(new EnvHttpProxyAgent())` (+ `undici` as a dep) — for the
   **cloud** path (Anthropic/OpenAI at :443). This **replaces claurst's fork**: no
   forked binary, no rebuild, no release pin. ✓ cheaper than claurst.
2. **The claurst-style plain-forward relay** on 127.0.0.1:11434 — for **local
   Ollama**, because undici can *only* CONNECT-tunnel and squid rejects CONNECT to
   :11434. occ points `OPENAI_BASE_URL=http://127.0.0.1:11434/v1` (NO_PROXY → direct
   to the relay → relay does a plain absolute-URI forward through squid). This is
   **not** eliminated — but danno already has it: `driver.py` `_OLLAMA_RELAY_SOURCE`
   / `_claurst_script` is provider-agnostic and reusable near-verbatim.

**Net: occ = claurst − fork (the relay stays).** Slightly less work than claurst (no
fork/rebuild/release-pin; the relay is already built), plus occ's own quirk that its
OpenAI path only works with streaming disabled (§6.0). Reproduce via the spike
scripts in `scratch/occ-spike-ws/` (host + sandbox).

## 5. Cloud reach — occ is multi-provider (corrected)

occ multiplexes providers by **model-name prefix** → matching base-URL/key env
(`detectProvider`, `agent-loop.mjs:263-267`):

| Model prefix | Provider path | Endpoint | Redirectable? |
|---|---|---|---|
| `claude-*` (default) | `callAnthropic` (`agent-loop.mjs:282,299`) | `api.anthropic.com` **hardcoded** | ❌ `ANTHROPIC_BASE_URL` declared but never read |
| `gpt-`/`o1`/`o3` | `callOpenAI` (`agent-loop.mjs:334,368`) | `OPENAI_BASE_URL` (defaults `api.openai.com/v1`) | ✅ OpenAI **or** any OpenAI-compat cloud **or** Ollama |
| `gemini*` | `callGoogle` (`agent-loop.mjs:388,406`) | `generativelanguage.googleapis.com` hardcoded | ❌ |

**There is no asymmetry between Ollama and cloud OpenAI** — both ride the *same*
`callOpenAI` + `OPENAI_BASE_URL` path; only the URL/key/egress differ:

| Target | `OPENAI_BASE_URL` | Key | Egress |
|---|---|---|---|
| Ollama (local) | `http://host.docker.internal:11434/v1` | dummy | the `:11434` hole |
| Real OpenAI | unset → `api.openai.com/v1` | real | general internet |
| NIM / Groq / DeepSeek (OpenAI-compat cloud) | their `/v1` | real | general internet |

If anything cloud OpenAI is *easier* through the sandbox than Ollama (general
internet is already open under `--policy allow`; Ollama needs the specific hole +
`host.docker.internal→localhost` rewrite).

**Corrected Cloud row:** occ cloud = **Anthropic API + OpenAI API + any
OpenAI-compatible cloud (NIM/Groq/DeepSeek/…) + Google Gemini**, multiplexed by
model-name prefix — *broader* than the `claude` baseline (Anthropic-only), and
overlapping both claurst's (NVIDIA NIM) and opencode's (NIM/raw) cloud reach. This
is what makes occ a *multi-provider* agent (claurst's shape), not a single-provider
baseline.

## 6. occ-specific wrinkles danno must handle

0. **Streaming MUST be disabled (`CLAUDE_CODE_STREAMING=0`).** occ's loop defaults to
   streaming (`agent-loop.mjs:113`) and does `for await (…response.events)`
   (`agent-loop.mjs:120`), but the OpenAI/Google provider paths are **non-streaming**
   — `callOpenAI` returns a plain `res.json()` object with no `.events`
   (`agent-loop.mjs:383`), so the default path crashes with `Cannot read properties
   of undefined (reading 'Symbol(Symbol.asyncIterator)')`. Only the Anthropic path
   implements streaming. **Verified required** on host and in-sandbox. danno must set
   `CLAUDE_CODE_STREAMING=0` in occ's env whenever the OpenAI path is used (i.e. any
   Ollama or OpenAI-compat target). (Filed as a candidate upstream occ bug.)
1. **`gpt-`/`o1`/`o3` prefix hack for the OpenAI path.** Any model not named
   `gpt-`/`o1`/`o3`/`gemini`/`claude-` routes to hardcoded `api.anthropic.com`
   (`agent-loop.mjs:263-267`). So **every** OpenAI-path target — cloud NIM `qwen`,
   Ollama `llama3` alike — needs danno to alias the model id to a `gpt-*` name. Real
   OpenAI models (`gpt-4o`) are frictionless; everything else on that path needs the
   alias. Contained, but real.
2. **Dummy `OPENAI_API_KEY` required** even for Ollama (`agent-loop.mjs:334`).
3. **Anthropic traffic is not redirectable/capturable** — `ANTHROPIC_BASE_URL` is
   ignored (`agent-loop.mjs:299`), so occ's Anthropic path can't be pointed at a
   `--capture` recording proxy (same limitation as the `claude` baseline). Its
   OpenAI-path traffic *can* be captured (rewrite `OPENAI_BASE_URL`), like opencode.
4. **Subagent frontmatter model is cosmetic** (§3) — drive model via `-m` only.

## 7. Corrections to the earlier clone research

`claude-code-clones-research.md` (2026-06-23) has two errors about occ, both from
reading its README rather than its source:

- **"Local models — NO … no Ollama or custom base-URL support."** Wrong: `callOpenAI`
  honors `OPENAI_BASE_URL` (`agent-loop.mjs:368`), so occ **can** reach Ollama and
  any OpenAI-compatible endpoint (subject to the §6 prefix/key gotchas). Only the
  *Anthropic* endpoint is truly fixed.
- **Cloud framed as Anthropic-only.** Understated: occ is multi-provider (§5).

The corrected disqualifier is *not* "off-thesis" but "**mostly redundant with the
`claude` baseline for the cloud A/B, and cheaper than claurst to add for the local
sweep**" — see the scoping split below.

## 8. Scoping: MVP vs full

- **MVP (cloud A/B):** add occ as a *second baseline* — occ vs real Claude Code on
  the same L0→L1→L2 battery, no sweep. Directly answers "compare open-claude-code to
  Claude Code." Still needs the §4 proxy shim (occ must reach `api.anthropic.com`).
- **Full (on-thesis):** add the local-Ollama (and OpenAI-compat cloud) sweep too,
  claurst-style — needs the proxy shim **plus** the §6 `gpt-*` model aliasing.

The proxy shim gates both, so **run the §4 spike first regardless.**

## 9. What a new agent must touch to follow the claurst pattern (claurst-lite)

Mirror the claurst seams, dropping config-gen and (hopefully) the relay/fork:

- **New host module `danno_validator/occ.py`** — mirror `claurst.py`:
  `OCC_SANDBOX_IMAGE = "shell"`, `install_occ()` (`npm i -g …` + version stamp for
  idempotent skip), `authed_occ_run()` `TurnFn`, launch/env helpers. **No fork
  binary** (vs `claurst.py:55` `CLAURST_RELEASE_URL`); **the proxy shim** replaces it.
- **`danno_validator/driver.py`** — `OccTurn` dataclass + `occ_run()` (mirror
  `ClaurstTurn`/`claurst_run` `driver.py:682-883`); pin flag constants; normalize
  occ's 13-event schema onto the `Turn` protocol (`driver.py:188`). **Reuse** the
  claurst relay source (`driver.py:100` `_OLLAMA_RELAY_SOURCE` / `_claurst_script`)
  as-is — required for local Ollama (§4 spike, verified). Always pass
  `CLAUDE_CODE_STREAMING=0` on the OpenAI path (§6.0).
- **`src/book_em_danno/commands/sandbox.py`** — `_docker_image()` branch
  (`sandbox.py:66`, `shell` image); `agent_env()` occ branch (`sandbox.py:392`,
  `HOME`/`OPENAI_BASE_URL`/`OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`NODE_OPTIONS`
  shim); `provision()` install hook (`sandbox.py:379-388`); model resolution +
  `gpt-*` aliasing (`sandbox.py:447-544`), incl. multi-provider key injection.
- **`danno_validator/suites/aut.py`** — add an `OCC` branch to `resolve_image`,
  `install_aut`, `run_turn_for` (`aut.py:21-64`) → `danno bench --agent occ` works.
- **`danno_validator/run.py:313-341`** — `is_occ` branch (image, install,
  `make_run_turn`) mirroring `is_claurst`.
- **`danno_validator/benchmark.py:196-201`** — no edit; the non-opencode guard
  rejects occ from `benchmark` for free (redirect to `danno bench --agent occ`).
- **`src/book_em_danno/cli.py`** — extend `_AGENT_OPT` (`cli.py:431`), `validate`
  /`bench` `--agent` help, `_MODEL_OPT` help.
- **Config generation** — skip almost entirely. Model via `-m`; optionally a thin
  `settings.json` `model` pin. None of the `generate_claurst_*` family is needed.

## 10. Open questions before implementing

1. **[SPIKE — RESOLVED 2026-07-02]** The undici shim carries **cloud** (:443 CONNECT
   ✅) but **not** Ollama (:11434 CONNECT rejected ❌) — local still needs the claurst
   relay. So **occ = claurst − fork**. Also discovered: `CLAUDE_CODE_STREAMING=0` is
   mandatory for occ's OpenAI path (§4, §6.0). Repro scripts in
   `scratch/occ-spike-ws/`.
2. MVP baseline-only or full sweep (§8)?
3. Version pin: which occ tag/commit does danno install (occ is a fast-moving
   `npx`/`main` project; the `stream-json` schema is not yet stable)?
4. Is a second `--baseline`-style flag wanted (occ vs claude side-by-side), or is
   occ just another `--agent occ` selection?
