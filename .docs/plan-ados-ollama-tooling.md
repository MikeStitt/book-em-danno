# Plan & status: ADOS-on-Ollama tooling

> **Living document.** This is the GitHub-visible plan and progress tracker for the
> ADOS-on-Ollama tooling. It is updated and committed as work proceeds. `.docs/` is
> **exempt from markdown/format quality checks** (see `.markdownlintignore` /
> `.prettierignore`) so frequent plan commits are never blocked — a noisy git
> history here is intentional and accepted.
>
> The canonical approved plan also lives at
> `~/.claude/plans/please-examine-agentic-delivery-os-i-refactored-sphinx.md`; this
> copy is the one tracked in git.

## Status

Legend: `[ ]` todo · `[~]` in progress · `[x]` done

- [x] Design plan approved — 2026-06-05
- [x] UX north-star doc — `.docs/user-experience-elephant.md`
- [x] Plan mirrored to `.docs/` + `.docs/` marked lint/format-exempt
- [x] **M0 — Repo foundation & quality gate** (`make check` green locally)
  - [x] `Makefile` (`check` = shellcheck + `shfmt -d` + `scripts/test-all.sh`; `test`; `develop`)
  - [x] `.shellcheckrc` (`enable=all`, disable SC2312)
  - [x] `scripts/test-all.sh` aggregator (bats; 0 if none found)
  - [x] `.pre-commit-config.yaml` (prek) — **excludes `.docs/`** (not yet exercised; prek run pending)
  - [x] `scripts/setup-dev.sh` (brew: bash shellcheck shfmt bats-core) — bash 3.2-safe bootstrap
  - [x] CI: GitHub Actions running `make check` on **macOS + Linux** (validates on first push)
  - [x] first bats smoke test (`scripts/.tests/setup-dev.bats`, 4 tests)
- [x] **M1 — Preflight doctor** — `tools/ados-ollama-doctor` (read-only deps check) + shared `scripts/lib/common.sh`
- [x] **M2 — Ollama provisioning** — `scripts/setup-ollama.sh` (`--model`, pull Gemma, verify responds + tool-probe)
- [x] **M3 — ADOS install integration** — `scripts/install-ados.sh` (global + project-local; record SHA)
- [x] **M4 — Hybrid config generator** — `tools/gen-opencode-config` (target `.opencode/opencode.jsonc`)
- [x] **M5 — Docker sandbox launcher** — `tools/ados-sandbox` (spike done; see findings below)
- [ ] **M6 — Orchestrator** — `tools/ados-ollama` (`doctor|setup|install|configure|sandbox|all`)
- [ ] **M7 — (later) macOS Seatbelt sandbox** — `tools/ados-sandbox-macos`
- [ ] **M8 — Docs + constitution sync** — README, usage guide, amend `parts/ados-ollama.md`

## Progress log

- **2026-06-05** — Plan approved. UX doc written (`user-experience-elephant.md`).
  Plan mirrored here; `.docs/` marked exempt from markdown/format checks. Work
  starting on M0.
- **2026-06-05** — M0 done: `make check` (shellcheck + shfmt + bats) green locally;
  installed brew bash 5.3.12 / shellcheck 0.11 / shfmt 3.13 / bats 1.13. Pending live
  validation: GitHub Actions CI (on push) and prek hook run. Starting M1.
- **2026-06-05** — M1 done: `tools/ados-ollama-doctor` + shared `scripts/lib/common.sh`;
  9/9 bats green. Live run on this host: all required PASS except Gemma model (FAIL,
  fixed by M2); docker + `docker sandbox` + ollama + opencode all present. Starting M2.
- **2026-06-05** — M2 done: `scripts/setup-ollama.sh`; 14/14 bats green. Exercised live:
  pulled `gemma3:1b`, verified it responds (num_ctx 32000). **Finding:** `gemma3:1b`
  did NOT tool-call on the probe → confirms the plan risk. Tool-heavy local agents
  (runner/committer) need a larger/tool-capable Gemma or stay cloud; M4's default map
  keeps this conservative + table-driven. Starting M3.
- **2026-06-05** — M3 done: `scripts/install-ados.sh`; 19/19 bats green incl. a live
  functional test that ran ADOS's installer into a temp git repo, copied agent/command
  defs project-local (the bit ADOS only does globally → needed for sandbox visibility),
  wrote `.opencode/ados-provenance.txt`, and confirmed idempotent re-run. Starting M4.
- **2026-06-05** — M4 done: `tools/gen-opencode-config`; 27/27 bats green. Writes the
  target `.opencode/opencode.jsonc` (ollama provider + 15 cloud / 5 local agent map,
  default_agent pm, num_ctx 32000, baseURL via {env:OLLAMA_BASE_URL}). Verified: valid
  JSON (python json.tool), idempotent re-run, preserve-on-diff without --force. baseURL
  env-token + whether OpenCode loads project `.opencode/opencode.jsonc` inside the sandbox
  remain M5 risks to verify. Starting M5.
- **2026-06-05** — M5 done: `tools/ados-sandbox`; 33/33 bats green. **Spike findings
  (live):** `docker sandbox create --name <n> opencode <ws>` works; the VM resolves
  `host.docker.internal` -> 192.168.65.254; **host Ollama binds 127.0.0.1 by default so
  the VM can't reach it -> must set `OLLAMA_HOST=0.0.0.0:11434`** (launcher detects +
  warns). No per-sandbox CPU/mem flag (Docker Desktop ▸ Resources). Egress is proxy-gated
  (`docker sandbox network proxy --allow-host`). Also fixed `run_cmd` to space-join
  dry-run output (was \n-joined via IFS). **Not yet verified (needs user):** full
  interactive `@pm` session in-sandbox — requires cloud keys + a tool-capable Gemma (27b)
  + confirming env injection via `exec --env-file`. Starting M6.

---

## Context

`claude-setup-agentic-delivery-os` is the Bash-first harness whose job (per its
constitution, v1.0.0) is to install and adjust Agentic Delivery OS (ADOS,
`../agentic-delivery-os`, OpenCode-based, MIT) onto target projects (e.g.
`mesh-atlas`, the `bench-*` runs) with a hybrid local+cloud model runtime. The
constitution is done; this plan builds the tooling. The repo currently has no
scripts, no `tools/`, no `make check` — only `.specify/`, `.claude/`, `.docs/`,
and the gitignore.

**Why / what it must achieve.** ADOS ships agent/command behavior but no
model/provider config and no Ollama/sandbox wiring — `install.sh` copies only
`.opencode/{agent,command}/*.md` to the global `~/.config/opencode`. We supply the
missing layer: a local Gemma runtime + cloud fallback + an isolated execution
sandbox, installed into a target project.

### Decisions locked

- **Agent runtime = Docker (Linux microVM) sandbox, sized generously.** Heavy
  C++/CGAL builds + large-STL processing run in the VM (Linux-target, near-native
  arm64 CPU). Isolation is the priority; the resource tax is accepted.
- **Ollama (Gemma) runs on the host** (Metal GPU / unified memory), not in the VM
  (no GPU passthrough on Mac; inference is the only GPU-hungry part). The sandboxed
  agent reaches it over the host gateway.
- **Hybrid mapping:** local `ollama/<gemma>` for tiers 3–5 (`committer`, `runner`,
  `external-researcher`, `image-generator`, `image-reviewer`); cloud for tiers 1–2
  (`architect`, `reviewer`, `plan-writer`, `pm`, `doc-syncer`, `toolsmith`,
  `coder`, `fixer`, `spec-writer`, `test-plan-writer`, `pr-manager`).
- **Model config is project-level:** target repo's `.opencode/opencode.jsonc` (the
  only config a Docker sandbox sees; committed; secrets via `{env:...}`).
- **ADOS agent/command defs must also be installed project-local**, because the
  sandbox can't see host `~/.config/opencode`.
- **"Both platforms" = our tooling + bats tests run on macOS (brew bash 5) and
  Linux** (CI matrix). A macOS (Seatbelt) agent sandbox is a later milestone for
  macOS-native debugging only (weaker isolation, deprecated API).

### Runtime architecture

```
Host macOS (native, Metal)        Ollama -> Gemma           full GPU + unified RAM
        ^ host-gateway :11434
Docker Linux microVM (isolated)   OpenCode + ADOS agents    allocated cores/RAM; heavy builds
        |   reads project .opencode/{opencode.jsonc,agent,command}
        +-- network allow: host Ollama + cloud model APIs + GitHub MCP
Cloud                              tier 1-2 models
```

## Conventions to reuse

- Mirror `../agentic-delivery-os/scripts/install.sh`: `log_*` + `LOG_TAG`,
  `run_cmd` (dry-run), `require_cmd`, `copy_file_with_diff` / idempotent merge,
  `ensure_gitignore_entry`, exit-code contract, testable `main` guard, env-var DI.
- Bash standard + layout from `.specify/memory/parts/bash.md`; tests per
  `testing.md`; hooks/CI per `shared.md`.
- Per the constitution: branch per milestone, conventional commits, `make check`
  green before done, `--dry-run` on every mutating script, idempotent + preserve
  project-specific files.

## Milestones (detail)

**M0 — Repo foundation & quality gate.** `Makefile` (`make check` = shellcheck +
`shfmt -d` + `scripts/test-all.sh`; `test`, `develop`); `.shellcheckrc`
(`enable=all`); `scripts/test-all.sh` aggregator; `.pre-commit-config.yaml` (prek)
that excludes `.docs/` from markdownlint/prettier; `scripts/setup-dev.sh` (brew:
bash shellcheck shfmt bats-core); GitHub Actions running `make check` on macOS +
Linux; first bats smoke test.

**M1 — Preflight doctor** `tools/ados-ollama-doctor` (read-only): verify brew bash
>=5, docker daemon up + `docker sandbox` present, `ollama` running, `opencode`, the
Gemma model pulled, and lint/test tools. Report misses with fix hints.

**M2 — Ollama provisioning** `scripts/setup-ollama.sh`: ensure Ollama running;
`--model` (default a Gemma tag, resolved against `ollama list`/pull); verify it
responds and tool-calls with `num_ctx` raised.

**M3 — ADOS install integration** `scripts/install-ados.sh`: wrap ADOS
`install.sh --global` (idempotent), then install ADOS agents/commands + docs
project-local into the target (`.opencode/agent`, `.opencode/command`, `doc/`,
`.ai/`) so the sandbox sees them; record the source ADOS git SHA (provenance).

**M4 — Hybrid config generator** `tools/gen-opencode-config`: generate/merge the
target `.opencode/opencode.jsonc` deterministically (idempotent; diff-preserve hand
edits; clearly-owned section):

- `provider.ollama` -> `@ai-sdk/openai-compatible`, `baseURL`
  `{env:OLLAMA_BASE_URL}` (default `http://localhost:11434/v1`; the sandbox sets the
  host-gateway URL), `options.num_ctx: 32000`, the Gemma model.
- `agent.<name>.model` for all agents from the tier->model table (`ollama/<gemma>`
  vs cloud); `default_agent: pm`. Keys are `{env:...}` only — no secrets in the file.

**M5 — Docker sandbox launcher** `tools/ados-sandbox` (highest-unknown; includes a
hands-on spike): wrap `docker sandbox` to run OpenCode for the target with generous
`--cpus/--memory` sizing, project working tree on the VM fs, network policy allowing
host Ollama + cloud APIs + GitHub MCP, `OLLAMA_BASE_URL=<host-gateway>`, and injected
creds (GitHub token, provider API keys) — never committed.

**M6 — Orchestrator** `tools/ados-ollama` (command pattern:
`doctor|setup|install|configure|sandbox|all`) tying M1–M5 into one idempotent,
`--dry-run`-able entry point with `--target <dir>` and resource flags.

**M7 — (later) macOS Seatbelt agent sandbox** `tools/ados-sandbox-macos`:
`sandbox-exec` + brew bash for macOS-native debug runs. Weaker, deprecated
isolation; documented as such.

**M8 — Docs + constitution sync.** README + usage guide; amend
`.specify/memory/parts/ados-ollama.md` (governed, version bump per
`constitution-maintenance.md`) to record the settled design, **and record the
`.docs/` lint/format exemption in `parts/shared.md`** so the documented rule matches
enforcement.

## Risks to verify, not assume (drive M5 + M2)

- Exact `docker sandbox create/run/exec/network` invocation to launch OpenCode with
  sizing + project mount + creds (Docker's published docs are thin).
- Host-gateway hostname reachable from inside the `docker sandbox` microVM to hit
  host Ollama `:11434` (host.docker.internal vs a gateway IP vs `docker sandbox
  network` config).
- That `.opencode/opencode.jsonc` and project-local `.opencode/{agent,command}` are
  actually loaded by OpenCode inside the sandbox.
- Gemma tool-calling reliability for the tool-heavy local agents (`runner`,
  `committer`): if weak, move them to cloud or pick a tool-capable Gemma — keep the
  split table-driven so it's a one-line change.

## Verification (end-to-end, on a throwaway clone of a `bench-*` repo)

1. `tools/ados-ollama-doctor` -> all green (or actionable misses).
2. `tools/ados-ollama all --target <clone> --dry-run` prints a converging plan; real
   run installs ADOS (global + project-local), pulls Gemma, writes
   `.opencode/opencode.jsonc`.
3. Idempotency: re-run `all` -> reports unchanged/no-op; `git diff` in target empty
   on the second pass.
4. `tools/ados-sandbox --target <clone>` launches OpenCode in the VM; from inside,
   confirm host Ollama is reachable and a local-tier agent answers via `ollama/...`.
5. Drive `@pm` through a trivial change: a cloud-tier agent (`coder`/`reviewer`) + a
   local-tier agent (`committer`/`runner`) both run; PR/branch produced.
6. Isolation check: the agent cannot read/write host paths outside the mounted
   project.
7. `make check` green on macOS and Linux.

## Out of scope (now)

- Tuning Gemma model size/quant beyond a sane default (M2 flag).
- Non-Ollama local backends (LM Studio, llama.cpp) — the generator stays
  provider-tabled so they're addable later.
- Tracker (`pm-instructions.md`) and `/bootstrap` content — owned by ADOS, run after
  install.
