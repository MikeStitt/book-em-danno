# Plan: claurst upstream PRs, a danno-pinned fork, and first-class danno↔claurst integration

_Companion to `.docs/claurst-integration.md` (the findings). This is the **execution plan**
for (1) upstreaming the claurst fixes as small independent PRs, (2) maintaining a fork that
carries every fix danno needs, and (3) elevating claurst to opencode's level of abstraction —
configured from `danno.toml` and run through every danno command._

Upstream repo: `github.com/kuberwastaken/claurst`. Working clone (already patched + warm build
cache): `/Users/mikestitt/projects/temp/claurst-src`. Bug numbers below = the bugs in
`.docs/claurst-integration.md`.

---

## Part 1 — Independent upstream PRs to claurst

**Principle:** one concern per PR, each with a standalone bug report + fix + verification, small
enough to review/merge on its own. Two source files each host two bugs, so the file-touch
matrix below flags the only overlaps; those pairs touch **disjoint functions** and are
independently mergeable — submit in the listed order so the second is a trivial rebase.

### File-touch matrix
| PR | Bug | Files (region) | Overlaps |
|----|-----|----------------|----------|
| **PR-1** | 5 | `api/.../providers/openai_compat_providers.rs` (`ollama()`, `nvidia()`) | none |
| **PR-2** | 7 | `query/src/lib.rs` (caps/tools, ~1115–1129) | PR-3 (same file, different fn) |
| **PR-3** | 1 | `query/src/lib.rs` (stall timer) + a config field | PR-2 (same file, different fn) |
| **PR-4** | 6 | `tui/src/app.rs` (`TurnComplete`, ~5982) | PR-5 (same file, different fn) |
| **PR-5** | 4 | `tui/src/app.rs` (`refresh_context_window_size`) + `core/.../message_utils.rs` | PR-4 (same file, different fn) |
| **PR-6** | 3 | `tui` ratatui draw/scroll path | none (issue-first) |
| — | 2 | _none — `max_turns` default is config, not a bug_ | optional `--help`/docs note |

### PR-1 — Request streamed token usage for Ollama (and NVIDIA NIM)
- **Report:** the context meter's "used" counter never moves with the Ollama/NVIDIA providers.
  `OpenAiCompatProvider` only sends `stream_options:{include_usage:true}` when the per-provider
  `include_usage_in_stream` quirk is set (`openai_compat.rs:505`); `ollama()` and `nvidia()`
  leave it at its `false` default, so no usage chunk is returned → `context_used_tokens` stays 0.
- **Fix:** set `include_usage_in_stream: true` in both providers.
- **Verify:** wire capture shows `stream_options.include_usage`; Ollama/NIM return a final
  `usage` chunk (both confirmed in the spike).

### PR-2 — Don't silently disable tools for models missing from the catalog
- **Report (highest impact):** any model not in the bundled models.dev catalog runs with **no
  tools** — `query/src/lib.rs:1116-1124` overrides `caps.tool_calling` with the registry
  entry's value, and a registry miss (or an entry without `tool_call`) yields `false`, so
  `provider_tools` becomes empty (`:1125`). The model only sees tools as system-prompt prose
  and narrates them as text → 1-turn end. (Cost us a whole "models can't drive claurst" false
  conclusion.)
- **Fix:** on a registry **miss**, keep `provider.capabilities()` (default `true` for
  openai-compat) instead of forcing `false`; optionally warn when a tool-using agent is about
  to send zero tools.
- **Verify:** captured request carries `tools`; model emits structured `tool_calls`.

### PR-3 — Make the provider stall timeout configurable (and longer by default)
- **Report:** `provider_stall_timeout`/`STALL_TIMEOUT` are hard-coded 45 s; a slow local
  prefill (98–157 s measured) trips it, retries exhaust, the turn is built from incomplete
  stream data, tool blocks are dropped → premature `EndTurn` "stall". Distinct from PR #187
  (the already-600 s reqwest HTTP timeout).
- **Fix:** make it a config/env knob (default ≥ 600 s).
- **Verify:** long-prefill turns survive (`STALL FIRED = 0` across a long run — confirmed).

### PR-4 — Fix the context-meter "used" math (set, don't accumulate)
- **Report:** `app.rs:5982` does `context_used_tokens += (input+output+cache)` every turn, but
  `input_tokens` already counts the whole growing history, so the meter balloons past the window.
- **Fix:** **set** `context_used_tokens` to the latest turn's prompt+output.
- **Verify:** meter tracks real occupancy (≈ half a 64 K window at ~200 msgs in the spike).

### PR-5 — Correct context-window size for Ollama/local models
- **Report:** `refresh_context_window_size` (and `message_utils::context_window_for_model`)
  resolve the window from the models.dev catalog; a local/custom model misses → wrong fallback
  (128 K shown vs a 64 K `num_ctx`-capped model).
- **Fix:** for `ollama/*`, read the effective `num_ctx` from Ollama `/api/show`
  (`parameters.num_ctx`, falling back to the catalog). (danno can also feed it via a registry
  overlay — see Part 3 — but the upstream fix is the general one.)
- **Verify:** meter denominator matches the model's real window.

### PR-6 — TUI scroll repaint ghosting (issue first)
- **Report:** scrolling the terminal leaves ghost/overlapping text — vacated cells not cleared
  on scroll repaint in the ratatui TUI.
- **Action:** file a detailed issue with repro (Ghostty, `Ctrl-L`/resize workarounds) before a
  fix PR; the draw-path fix is larger and less certain than PRs 1–5.

---

## Part 2 — The danno-pinned claurst fork

**Goal:** a fork that always carries *every* fix danno relies on, so danno builds/pins a known-
good claurst even before the PRs land upstream.

1. **Fork**: `gh repo fork kuberwastaken/claurst --clone=false` → `MikeStitt/claurst`.
2. **Branches**:
   - `danno-integration` — the **union of PRs 1–5** (PR-6 when ready). This is what danno builds.
   - Each upstream PR branches off `main` and is cherry-picked from here, so the fork shrinks as
     PRs merge.
   - `danno-debug` (optional) — the env-gated diagnostics (`CLAURST_LOOP_LOG` trace,
     `CLAURST_DUMP_REQUEST`); harmless and off-by-default, so they may instead just ride on
     `danno-integration`.
3. **Release artifact**: build `claurst-linux-aarch64` from `danno-integration` (the Docker
   recipe in `.docs/claurst-integration.md`, ~1 min warm) and attach it to a fork release tagged
   e.g. `v0.1.6+danno.1`.
4. **danno consumes it**: point `src/danno_validator/claurst.py` (`CLAURST_VERSION`,
   `CLAURST_RELEASE_URL`) at the fork release, or build-from-source pinned to a commit. The
   installer already skips reinstall when the version matches, so the `+danno.N` suffix lets
   danno detect/refresh deliberately.
5. **Maintenance**: as PRs merge upstream, rebase `danno-integration` onto upstream and drop the
   merged commits; when all land, retire the fork (or keep only PR-6).

---

## Status — fork + branches CREATED (2026-06-27)

Fork: **`github.com/MikeStitt/claurst`** (`upstream` = `Kuberwastaken/claurst`). All branches
pushed. **PRs not opened yet** — per the rule, we build/test each before PR.

| Branch | Bug | SHA | Code status |
|---|---|---|---|
| `danno-integration` | union | `123c8c5` | **TESTED** — this is the binary built/installed/ran the NIM spike (Bug 5 + 6 + 1-as-600s + env-gated diagnostics). The branch danno builds/pins. |
| `fix/ollama-nvidia-stream-usage` | 5 | `f384b57` | tested (extracted from the spike binary) |
| `fix/context-used-set-not-accumulate` | 6 | `5e2a4a2` | tested (extracted from the spike binary) |
| `fix/configurable-provider-stall-timeout` | 1 | `c435699` | **candidate** — new code (`CLAURST_PROVIDER_STALL_TIMEOUT_SECS`, default 600); not yet compiled |
| `fix/warn-on-zero-tools` | 7 | `f07c81c` | **candidate** — new code (loud warn on 0 tools); not yet compiled |
| `fix/ollama-context-window-fallback` | 4 | `edfc2bd` | **candidate** — new code (conservative local default); not yet compiled |
| `fix/tui-scroll-repaint-ghosting` | 3 | `f67f33c` | **issue-first** — documents the bug (`docs/known-issues/…`); no code fix yet |

**Pre-PR gate (todo):** build each candidate branch (`cargo build --release -p claurst`, warm
cache ~1 min) + run against the spike task; only then open PRs in dependency order (PR-1 first).
**`danno-integration` refresh (todo):** fold the cleaner `configurable-stall-timeout` + `warn-on-
zero-tools` + `context-window-fallback` commits in to replace the 600s-hardcode, re-verifying the
loop_log diagnostic doesn't conflict with the configurable-timeout hunk.

## Part 3 — First-class danno↔claurst integration (claurst at opencode's abstraction level)

### Current asymmetry
| Aspect | opencode (first-class) | claurst (special-case today) |
|---|---|---|
| Config source | **generated** `.opencode/opencode.jsonc` from `danno.toml` (`config/generate.py`) | **none** — hand-wired launch flags |
| Models | `[models]` → `model_ref()` → `<provider>/<tag>` + `limit`/`reasoningEffort` | `-m ollama/<tag>` only |
| Backends | `[backends]` → opencode provider blocks / catalog | implicit (OLLAMA_HOST); cloud assumed impossible |
| Agents | `[agents]` → marker-region model assignment (+ rich `AgentSpec`) | not mapped |
| Install | `danno install` provisions + generates config | binary download only (`claurst.py`) |
| Commands | install / validate / bench / benchmark / sandbox | sandbox (+ validate as local-Ollama HUT) |

**Key correction to bake in:** `danno_validator/claurst.py:12-13` asserts claurst "ignores the
proxy, so cloud-backed variants cannot reach their providers." **The spike disproved this** —
claurst (reqwest) honored `HTTPS_PROXY` and reached `integrate.api.nvidia.com` (HTTP 200/401).
So cloud claurst is viable, which is what makes "claurst at opencode's level" possible. First
task of Part 3 is to **re-verify proxy honoring** and lift the local-only restriction.

### Target
`danno.toml` drives claurst exactly as it drives opencode: the same `[backends]`/`[models]`/
`[agents]` produce a generated claurst configuration, and `--harness claurst` works across
install / validate / bench / benchmark / sandbox.

### Config-generation design (`danno.toml` → claurst artifacts)
A `generate_claurst_config()` sibling to the opencode generator, emitting into the relocated
claurst HOME (`~/.claurst`) / `CLAURST_MODELS_PATH`:
- **Models → a claurst model-registry overlay** (the `nvidia-models.json` shape proven in the
  spike): per model `provider/tag`, **`tool_call: true`** (Bug 7), `limit.context` (Bug 4/5),
  `reasoning` for thinking models. This is the same `[models]` data already resolved by
  `model_ref()`. Pointed at via `CLAURST_MODELS_PATH`.
- **Backends → providers + env**: `ollama` (OLLAMA_HOST→relay), `nvidia` (`NVIDIA_API_KEY` via
  the chmod-600 env-file path danno already uses for Claude auth), generic `openai`-compat
  (base_url + `api_key_env`). Reuse danno's auth-injection + `--capture` proxy wiring.
- **Agents → claurst agent defs**: map `[agents]`/`AgentSpec` (model, mode, permission, steps→
  `max_turns`, prompt) to claurst's agent-definition format (markdown/settings). Respects the
  Bug-2 `max_turns` lever.

### Per-command integration
- **install**: install the (forked) binary **and** run `generate_claurst_config()` from
  `danno.toml` (today install ignores claurst). Advise-by-default / `--apply` like everything else.
- **sandbox**: extend the existing `--harness claurst` path to load the generated config + support
  cloud backends (post proxy re-verification); keep HOME relocation + relay + WAL handling.
- **validate**: the validator already drives claurst as a local-Ollama HUT
  (`[[claurst-as-coding-tool]]`); extend the matrix to cloud models via the generated registry +
  proxy, so claurst sweeps the same L0/L1/L2 battery opencode does.
- **bench / benchmark**: run claurst as the harness with the generated per-config `.claurst`
  context, mirroring how `benchmark` gives each opencode candidate its own `.opencode/`.

### Dependencies & sequencing
1. **Part 1/2 first** — the integration *requires* Bug-7 (tools) and Bug-5/4/6 (meter) fixes;
   danno builds them via the fork. Without Bug 7, a generated claurst config still can't tool-call.
2. **Re-verify proxy honoring** → lift local-only restriction in `claurst.py`.
3. **Milestones:**
   - **M0** — fork + `danno-integration` build wired into `claurst.py` (Part 2).
   - **M1** — `generate_claurst_config()` (models overlay first; the spike's `nvidia-models.json`
     is the prototype) + `install`/`sandbox` consume it; cloud claurst working through the proxy.
   - **M2** — agents mapping (`AgentSpec` → claurst agent def, incl. `max_turns`).
   - **M3** — `validate` cloud matrix for claurst.
   - **M4** — `bench`/`benchmark` with claurst as a first-class agent.
   - **M5** — docs: README + `--help` describe claurst as a peer of opencode (Documentation Hygiene).

### Open questions to resolve during M0/M1
- Does claurst honor a config-dir override, or only `~/.claurst`? (Today: only HOME — verified
  0.1.5; re-check on the fork.) Determines whether per-config isolation needs HOME relocation per run.
- claurst's native agent-definition format + whether `CLAURST_MODELS_PATH` + agent defs fully
  replace its interactive `settings.json` generation (deferred in `[[claurst-as-coding-tool]]`).
- Capture parity: does `--capture` already cover claurst↔cloud (not just the Ollama relay)?
