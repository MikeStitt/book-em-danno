# sbx migration — end-to-end validation (2026-07-11)

**Result: PASS.** danno drives all four harnesses through a real `sbx` microVM
sandbox against host Ollama, and the egress boundary holds. This is the plan's
**W2 verification gate** (see [`plan-sbx-migration.md`](plan-sbx-migration.md)),
run post-W1 (the loopback-resolver retirement).

## Environment

- `sbx v0.34.0` on macOS (Docker Desktop still ships legacy `docker sandbox`; the
  dual-CLI window holds). Backend auto-selected `sbx` (installed → preferred).
- Host Ollama, model **`gpt-oss:20b`** (tool-capable, loads reliably alongside the
  microVM; `reasoning_effort = "low"` — gpt-oss rejects `"none"`).
- danno `0.14.0` + this branch. Disposable validator-owned sandboxes; the config
  under test is in scratch (a local-only `danno.toml`, backend **named `ollama`** —
  see "Findings" #1).

## Per-harness result — `danno validate … --max-level 1` (L0 liveness · L1 tool/bash)

| Harness | how it reaches Ollama | L0 | L1 | notes |
|---|---|---|---|---|
| **opencode** | relay-free (Bun fetch honors the injected proxy) | ✓ 15.5s | ✓ 6.4s | `--harness opencode` |
| **claude code** | own OAuth to `api.anthropic.com` (in `balanced`'s default-ai-services) | ✓ 13.4s | ✓ 9.0s | `--baseline --baseline-model haiku` |
| **occ** | in-sandbox relay (`OPENAI_BASE_URL` → relay → `host.docker.internal`) | ✓ 8.5s | ✓ 19.7s | `--harness occ`; git-clones + `npm i undici` in-VM |
| **claurst** | in-sandbox relay (`OLLAMA_HOST` → relay upstream) | ✓ 10.5s | ✓ 19.2s | `--harness claurst`; curl-installs its binary in-VM |

occ + claurst run over the **unchanged relay** (plan W2's stated path; relay-free
W3/W4 remain follow-ups). opencode + claude are already relay-free.

## Egress boundary probe (the security half of W2)

From inside a danno-provisioned `sbx` sandbox (egress = `balanced` + only the
per-sandbox Ollama allow), reading **HTTP status codes** per W7 (sbx deny = 403,
never exit codes):

| target | expected | observed |
|---|---|---|
| `host.docker.internal:11434/api/tags` (Ollama, allowed) | 200 | **200** ✓ |
| `example.com` (external) | deny | **403** ✓ |
| `10.0.1.9` (host LAN IP) | deny | **403** ✓ |
| `10.5.0.2` (default gateway) | deny | **403** ✓ |
| `host.docker.internal:22` (other host port) | deny | **403** ✓ |

Proxy env in-sandbox: `HTTP(S)_PROXY=http://gateway.docker.internal:3128` — the
documented sbx model. This confirms **W1**: `host.docker.internal` is rewritten to
`localhost` before matching, so the default `localhost:11434` allow token reaches
Ollama (200) while everything else is default-denied (403). The old
`127.0.0.1:11434` resolver token would have 403'd the Ollama path.

## Findings (harness-leg issues surfaced, orthogonal to the sbx backend)

1. **occ + claurst require the local Ollama backend be named `ollama`.** The
   validate sweep passes `<backend>/<tag>` refs, and both drivers key
   `is_local`/relay routing on the literal `ollama/` prefix
   (`driver.occ_model_target`, `driver.py:921`, `claurst.py:148`). A backend named
   e.g. `danno-ollama` yields `danno-ollama/<tag>` → misrouted as cloud → `OpenAI
   API error 404 model_not_found`. opencode/claude are name-agnostic. This is the
   pre-existing PR-#68 "validate sweep shares latent bug" follow-up, **not** an sbx
   or W1 regression (repros on docker). Worked around here by naming the backend
   `ollama`; a deeper fix would make the drivers route on backend **kind**, not name.
2. **claurst install raced the shell VM's boot-time apt lock** → fixed on this
   branch (fuser-wait; commit `fix(claurst): wait out the shell VM's boot-apt
   lock…`). Image-level race, so the fix hardens `docker sandbox` too.

## What this does NOT cover (still-open plan items, all relay/optimization follow-ups)

- **W3/W4** relay-free claurst/occ (spikes S1/S2) · **W5** timeout parity on
  relay-free paths · **W6** capture rewiring · **S3** loopback-only Ollama · **S4**
  egress-posture decision (owner: user). None block "danno runs under sbx" — the
  relay path is verified working. Deferred `sbx secret` (D4) unchanged.
