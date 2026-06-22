# claude-setup-agentic-delivery-os Constitution

This Constitution is **authoritative** for development practices in this
repository. It supersedes ad-hoc conventions, verbal agreements, and any
conflicting guidance in `CLAUDE.md`, `AGENTS.md`,
`.github/copilot-instructions.md`, or sub-directory READMEs — those files are
thin pointers back here.

**These rules apply to every task unless explicitly overridden.** Bias toward
caution over speed on non-trivial work; use judgment on trivial tasks.

This file is the **always-read core**: the Working Rules below plus Engineering
Discipline. Detailed stack and integration knowledge lives in
[`parts/`](#parts--read-only-what-your-task-needs) — read **only** the part(s)
for the work type you are touching.

## What this repository is

This repository holds **`danno`, a Python CLI** that declaratively provisions
**OpenCode hybrid local/cloud model runtimes inside a Docker sandbox**, driven by
a single `danno.toml`. From that one file `danno` generates the project's
auto-loaded `.opencode/opencode.jsonc`, ensures the local [Ollama](https://ollama.com)-served
models are pulled, installs a catalog of agentic tools, and creates a Docker
Desktop microVM sandbox wired to host Ollama — so cheap, high-volume agents run
locally on the developer's own machine (tuned for a MacBook Pro) while cloud
models run the high-stakes agents.

**ADOS** — [Agentic Delivery OS](https://github.com/juliusz-cwiakalski/agentic-delivery-os),
an MIT-licensed OpenCode-based delivery framework (local checkout at
`../agentic-delivery-os`) — is **one configurable tool in danno's catalog**, not
the reason this project exists. Installing it is a special case in the tool
installer (its agent/command defs must land project-local so the sandbox can see
them); see [`parts/ados-ollama.md`](parts/ados-ollama.md).

Because `danno` writes into **other people's repositories** (the target project)
and runs host/Docker/Ollama side effects, the defining discipline here is doing
so **non-destructively, idempotently, and transparently** — a two-tier policy
that advises (prints copy-paste commands) by default and executes only under
`--apply` (see the Working Rules below and [`parts/python.md`](parts/python.md)).

## Working Rules

The behavioral contract. Numbered for reference, not priority.

1. **Think before coding.** State assumptions explicitly. If uncertain, ask
   rather than guess. Push back when a simpler approach exists. Stop when
   confused.
2. **Simplicity first.** Write the minimum code that solves the problem.
   Nothing speculative; no features beyond what was asked; no abstraction for
   single-use code. Three similar lines beat a premature abstraction. If a
   simpler alternative exists, choose it unless you can document why not.
3. **Surgical changes.** Touch only what the task requires. Clean up only your
   own mess. Don't "improve" adjacent code, comments, or formatting. Match the
   existing style.
4. **Read before you write.** Before adding code, read the file's exports, its
   immediate callers, and the shared utilities it would use — so you don't
   duplicate what already exists. If unsure why code is shaped a certain way,
   ask. "Looks orthogonal" is a dangerous assumption.
5. **Goal-driven execution.** Define success criteria and loop until verified.
   Don't blindly follow rigid steps; define what success looks like and iterate
   toward it.
6. **Non-destructive, idempotent target installs.** This repo's defining rule.
   Installing or adjusting ADOS on a target project MUST be safe to re-run and
   MUST converge to the same result. Never clobber project-specific files (stock
   ADOS already preserves `.ai/agent/pm-instructions.md` and similar). Prefer a
   dry-run path, make surgical edits, and only ever touch a target repo under
   version control so the human can review and revert. Do not fork ADOS's agent
   or command behavior — we configure model assignment and install glue only.
7. **Small, bounded, side-effect-free.** Favor small composable functions with
   explicit inputs/outputs and clear boundaries; avoid god scripts. Keep core
   logic pure; I/O (filesystem, network, spawning `ollama`/`opencode`) lives in
   thin, mockable wrappers. Put validation at the boundaries (CLI args, target
   repo state, external commands), not for impossible internal states.
8. **Fail loud.** "Completed" is wrong if anything was skipped silently. "Tests
   pass" is wrong if any were skipped or pass for the wrong reason. Surface every
   skipped file, refused overwrite, and missing dependency. Default to surfacing
   uncertainty, never hiding it.
9. **Checkpoint long operations.** After each significant step in a multi-step
   task, summarize what was done, what is verified, and what is left. Don't
   continue from a state you can't describe back.
10. **Mind the budget.** On non-trivial work, watch the token/time budget. If a
    task is spiraling (e.g. debugging the same error repeatedly), stop,
    summarize, and restart fresh rather than overrun silently.
11. **Verify before done.** `ninja check` MUST be green before you declare a task
    complete. Tests are a first-class artifact (see
    [`parts/testing.md`](parts/testing.md)).

## Engineering Discipline

### Quality Gates

All code MUST pass `ninja check` before being committed. For this Python
repository the gate runs **ruff** (lint), **ruff format --check** (format),
**mypy** (types), and **pytest** (the fast suite; slow live tests are opt-in via
`-m slow`). The gate is defined in `build.ninja` and shells to `uv run …` so the
toolchain is the project's locked dependencies (see [`parts/python.md`](parts/python.md)
and [`parts/testing.md`](parts/testing.md)).

- Pre-commit hooks MUST remain active; never bypass them with `--no-verify`
  (hook details: [`parts/shared.md`](parts/shared.md)).
- CI reproduces these checks on every pull request.

### Configuration is Code

Infrastructure and configuration (hooks, linter/formatter configs, CI,
`build.ninja`, `danno.toml`, the generated auto-loaded `.opencode/opencode.jsonc`
model assignments, the install logic itself) are code, and a change to them is
not done until it has been **verified by exercising it**, not merely edited.
Define the success criterion as observed behavior and run the config to confirm
it: feed a deliberately-bad `danno.toml` through the loader and watch it reject;
run `danno install` against a throwaway target and confirm it converges; re-run
it and confirm it is a no-op. `ninja check` does not exercise every config (it
does not run the commit-msg hooks, nor a real install), so a silently broken
config can pass it — close that gap by hand.

### Branch & Push Policy

- **Branching**: Do work on a feature branch — never commit directly to `main`.
  Before staging the first change of any task, check
  `git branch --show-current`; if it returns `main`, run
  `git switch -c <kebab-case-name>`. One branch per logical unit.
- **Stack on unmerged work; don't force independence.** When a task builds on, or
  will touch the same files as, a branch/PR that has not merged yet, branch from
  *that branch* rather than `main`, and name the base in your summary. Do **not**
  rebase or re-create an already-stacked branch onto `main` to make its diff look
  "pure" — that is exactly what re-introduces the merge conflicts (lockfiles like
  `uv.lock`, `CHANGELOG.md`, shared modules) that stacking avoids. Reserve
  independent branches off `main` for work that is genuinely unrelated *and*
  touches disjoint files. When the right base is unclear, ask before branching.
- **The user owns the merge order**; you only push your branch. Do not merge PRs
  on the user's behalf.
- **Pushing**: Once `ninja check` is locally green and you're confident CI will
  pass, `git push -u origin <branch>` without asking. Never push to `main`;
  never force-push or rewrite published history without an explicit request.
- **Merging back**: via PR only.

### Conventional Commits

All commits MUST follow `<type>(<scope>): <subject>`.

- **Types**: `feat`, `fix`, `docs`, `chore`, `style`, `test`, `build`, `ci`,
  `refactor`, `perf`, `revert`.
- **Imperative mood**, **lowercase start** (unless proper noun/acronym).
- **Subject length**: ≤80 characters. **Body wrap**: at 80 characters.
- **Atomic commits**: one logical change per commit.
- `git-cliff` generates the changelog from these commits; commits are the source
  of truth (changelog mechanics: [`parts/shared.md`](parts/shared.md)).

### Documentation Hygiene

Any behavior-affecting change MUST update affected `--help` text, READMEs, and
related documentation in the same commit. A documentation gap is a bug. The
agent-doc files (`CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`) are
thin pointers — change a rule **here**, not in those pointers.

### Plan-File Etiquette

Plan files (`~/.claude/plans/*.md` and equivalent session-scoped planning
artefacts) are accumulated session memory. When entering plan mode for a task
that doesn't match existing plan content: **archive** the existing plan in place
(prefix its top heading with `# Archived plan (YYYY-MM-DD): <old title>`, keep
the body), **append** the new plan to the same file, and **never** overwrite
wholesale. Starting fresh is the user's decision (a new agent instance is how to
do that).

### ADOS provenance

ADOS is upstream MIT-licensed code we install and configure, not code we own.
When this tooling vendors or copies ADOS artefacts into a target project, keep
upstream license headers intact and record which ADOS version a target was
installed from so re-runs and updates are traceable. **Do not fork ADOS agent or
command behavior**: never edit the body (the system prompt) or the behavior fields
(`prompt`/`tools`/`mode`) of an agent `.md`. danno MAY, however, write **model
assignment** — its guaranteed lever — into a danno-managed, marker-delimited region
of an agent `.md`'s YAML frontmatter when that `.md` controls the agent (OpenCode
resolves a markdown agent def over the generated `.opencode/opencode.jsonc` on any
conflict, so the model would otherwise be silently shadowed there). Such edits are
**surgical** (only the marked region), **idempotent**, and **reversible**
(git-tracked, diff-then-stop without `--apply`) — the same Tier-1 discipline danno
applies to `.opencode/opencode.jsonc`. Details: [`parts/ados-ollama.md`](parts/ados-ollama.md).

### Scratch / Probe Scripts — Explicit Escape Hatch

Throwaway scripts written to investigate ADOS internals or a target repo SHOULD
NOT be held to the standards above. They live in `scratch/` (gitignored),
excluded from lint / format / tests; the hooks SHOULD NOT block on them.
**Promotion**: if a probe script proves repeatedly useful, port it into
`src/book_em_danno/` with full standards applied — do not let useful logic rot
in `scratch/`. This exception is named explicitly so future contributors don't
"tidy" it away.

## Parts — read only what your task needs

| Work type                              | Read                                                                     |
| -------------------------------------- | ------------------------------------------------------------------------ |
| Python CLI code (`danno`)              | [`parts/python.md`](parts/python.md)                                     |
| ADOS install/adjust + Ollama model cfg | [`parts/ados-ollama.md`](parts/ados-ollama.md)                           |
| Hooks / CI / changelog / cwd-flags     | [`parts/shared.md`](parts/shared.md)                                     |
| Writing tests                          | [`parts/testing.md`](parts/testing.md)                                   |
| Amending the constitution itself       | [`parts/constitution-maintenance.md`](parts/constitution-maintenance.md) |

## Governance

- **Compliance**: all pull requests and code reviews MUST verify adherence to
  these principles. Violations MUST be flagged and resolved before merge.
- **Amendment workflow & changelog**: the step-by-step amendment plan and the
  full dated version history live in
  [`parts/constitution-maintenance.md`](parts/constitution-maintenance.md).
  Read that part before changing this file or any other part.

**Version**: 2.2.0 | **Ratified**: 2026-06-05 | **Last amended**: 2026-06-22
