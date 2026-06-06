# Constitution — Maintenance part

Read this part **only** when you are amending the constitution itself
(`../constitution.md` or any file under `parts/`). It records the maintenance
plan — how amendments are made and versioned — and holds the full **Changelog**
moved out of the always-read core so that core stays lean. Read together with the
[constitution](../constitution.md)'s Governance section.

**Amendments**: any change to this constitution MUST be documented with a version
bump and rationale.

**Versioning**: semantic. MAJOR — principle removals or incompatible
redefinitions. MINOR — new principles or material expansions. PATCH — wording
clarifications and typo fixes.

## Maintenance plan

Amending the constitution is itself governed work; treat the rules below as the
checklist for any change to `constitution.md` or a part.

1. **Edit the right home.** Behavioral rules and the always-read core live in
   `constitution.md`; per-work-type detail lives in `parts/`. Change a rule in
   exactly one place — never duplicate it into the agent-doc pointers
   (`CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`).
2. **Bump the version.** Apply the semantic rule from Governance: MAJOR for
   principle removals or incompatible redefinitions, MINOR for new principles or
   material expansions, PATCH for wording clarifications and typo fixes. Update
   the `**Version**` and `**Last amended**` line in `constitution.md`.
3. **Record it in the changelog.** Add a dated entry to the
   [Changelog](#changelog) below — newest first — stating what changed and
   _why_. The rationale is the valuable part; a bare "updated X" is not enough.
4. **Update companions in the same commit.** If the change has an on-disk
   companion (e.g. a `.shellcheckrc`, hook, CI, or `.opencode` template that
   enforces the rule), change it in the same commit so the documented rule and
   its enforcement never drift apart.
5. **Exercise config companions, don't just edit them** (see the constitution's
   _Configuration is code_ rule). A rule whose enforcement lives in config is
   only amended once the config has been _run_ and shown to behave as intended —
   editing the file is not verification.

## Changelog

- **1.1.1 (2026-06-05)** — PATCH: DRY the networking docs. The Docker-sandbox
  network model (egress allow/deny, `host.docker.internal`→`localhost` rewrite,
  `OLLAMA_HOST=0.0.0.0` bind, baseURL) now lives **once** in the README's "Network
  model" section; `parts/ados-ollama.md` trims its `Ollama runtime` /
  `Docker sandbox runtime` sections to the design principles and links to the
  README. Also corrects stale detail (the old `{env:OLLAMA_BASE_URL}` baseURL is
  superseded by a baked `host.docker.internal` URL + dummy `apiKey` +
  `tool_call`/`limit`, which the OpenAI-compatible provider requires). No
  behavioral rule changed; this is a documentation relocation, with the README as
  the single home for operational network detail referenced from the part.
- **1.1.0 (2026-06-05)** — MINOR: synced the constitution to the as-built
  ADOS-on-Ollama tooling (milestones M0–M8) and added one new rule.
  - Rewrote [`parts/ados-ollama.md`](ados-ollama.md) to the **as-built** design:
    we own the project's auto-loaded `.opencode/opencode.jsonc` (OpenCode does not
    auto-load `opencode-<provider>.jsonc`); agent/command defs install
    project-local (a Docker sandbox can't see host `~/.config/opencode`); host
    Ollama must bind `0.0.0.0` (`OLLAMA_HOST=0.0.0.0:11434`) and the sandbox
    reaches it via `host.docker.internal`; `baseURL` via `{env:OLLAMA_BASE_URL}`;
    the empirical tool-calling caveat (`gemma3:1b` can't tool-call → local agents
    need a tool-capable Gemma); and the `docker sandbox` runtime (no per-sandbox
    CPU/mem flag — size in Docker Desktop). Added the as-built toolkit command
    table.
  - Added a `.docs/` document-quality exemption rule to
    [`parts/shared.md`](shared.md): markdownlint/Prettier skip `.docs/` (companions
    `.markdownlintignore`, `.prettierignore`, and the `exclude: ^\.docs/` in
    `.pre-commit-config.yaml`) so the living plan/status tracker commits freely.
    Companions exist and were exercised by inspection; full hook execution lands
    when `prek` is run.
- **1.0.0 (2026-06-05)** — Initial constitution for
  `claude-setup-agentic-delivery-os`. The structure (lean always-read core +
  per-work-type `parts/`, the Working Rules contract, Engineering Discipline) was
  adapted from the `mesh-atlas` constitution v3.0.0, then **retargeted** to this
  project's actual mission: Bash-first tooling that installs and adjusts Agentic
  Delivery OS (ADOS) onto target projects with a **hybrid local+cloud model
  runtime** (local Ollama-served Gemma for cheap/high-volume agents, cloud for
  high-stakes agents). A fresh v1.0.0 rather than an amendment of the mesh-atlas
  lineage because this is a different project with a different purpose; the
  mesh-atlas changelog (1.0.0→3.0.0) does not apply here.
  - Replaced the mesh-atlas identity/subproject narrative with the ADOS-installer
    mission; dropped the `benchmarks/` success-criterion paragraph.
  - Added a new core Working Rule — **non-destructive, idempotent target
    installs** — the defining discipline for tooling that edits other repos; and
    an **ADOS provenance** discipline (don't fork upstream, keep license headers,
    record installed version).
  - Replaced the language parts: deleted `python.md`, `typescript.md`, `cpp.md`
    (mesh-atlas-only); added `bash.md` (script/tool standards, citing ADOS's
    upstream `.ai/rules/bash.md`) and `ados-ollama.md` (install flow + the hybrid
    model mapping over OpenCode's `.opencode/opencode-<provider>.jsonc`).
    Retargeted `shared.md` (dropped the annotation-schema section; kept hooks/CI/
    changelog and the cwd rule) and `testing.md` (bats/embedded framework, single
    Bash home, idempotency/preservation behavior tests).
  - Quality gate is now `make check` backed by `shellcheck` + `shfmt -d` + the
    bats/embedded test suites (was the mesh-atlas multi-language `make check`).

## See also

- [`../constitution.md`](../constitution.md) — Working Rules, Engineering
  Discipline, and the Governance section this part expands.
- [`shared.md`](shared.md) — the `git-cliff` source-code changelog
  (`CHANGELOG.md`), a separate artefact from this constitution history.
