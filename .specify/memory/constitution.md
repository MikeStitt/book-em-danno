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

This repository holds **Bash-first tooling that Claude Code uses to install and
adjust [Agentic Delivery OS](https://github.com/juliusz-cwiakalski/agentic-delivery-os)
(ADOS) onto target projects** — for example `mesh-atlas` or the generated
`bench-*` benchmark runs that live alongside it. ADOS is an MIT-licensed,
OpenCode-based delivery framework (19 agents, 16 commands, a 10-phase lifecycle);
its local checkout is at `../agentic-delivery-os`. See
[`parts/ados-ollama.md`](parts/ados-ollama.md) for how it installs and how we
extend it.

What this tooling adds on top of stock ADOS is a **hybrid model runtime**:
local [Ollama](https://ollama.com)-served **Gemma** models run the cheap,
high-volume agents on the developer's own machine (this is tuned for a local
machine like a MacBook Pro), while cloud models run the high-stakes agents.
Stock ADOS ships no local-model configuration — generating that hybrid
assignment, and the glue to install it cleanly into a target repo, is this
project's reason to exist.

Because the deliverable installs and edits **other people's repositories**, the
defining discipline here is doing so **non-destructively and idempotently** (see
the Working Rules below).

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
11. **Verify before done.** `make check` MUST be green before you declare a task
    complete. Tests are a first-class artifact (see
    [`parts/testing.md`](parts/testing.md)).

## Engineering Discipline

### Quality Gates

All code MUST pass `make check` before being committed. For this Bash-first
repository the gate runs `shellcheck`, `shfmt -d` (format check), and the `bats`
/ embedded test suites (see [`parts/bash.md`](parts/bash.md) and
[`parts/testing.md`](parts/testing.md)).

- Pre-commit hooks MUST remain active; never bypass them with `--no-verify`
  (hook details: [`parts/shared.md`](parts/shared.md)).
- CI reproduces these checks on every pull request.

### Configuration is Code

Infrastructure and configuration (hooks, linter/formatter configs, CI, the
generated `.opencode/opencode-*.jsonc` model assignments, the install scripts
themselves) are code, and a change to them is not done until it has been
**verified by exercising it**, not merely edited. Define the success criterion
as observed behavior and run the config to confirm it: feed a deliberately-bad
input through the hook and watch it reject; run the installer against a throwaway
target and confirm it converges; re-run it and confirm it is a no-op. `make
check` does not exercise every config (it does not run the commit-msg hooks, nor
a real install), so a silently broken config can pass it — close that gap by
hand.

### Branch & Push Policy

- **Branching**: Do work on a feature branch — never commit directly to `main`.
  Before staging the first change of any task, check
  `git branch --show-current`; if it returns `main`, run
  `git switch -c <kebab-case-name>`. One branch per logical unit.
- **The user owns the merge order**; you only push your branch. Do not merge PRs
  on the user's behalf.
- **Pushing**: Once `make check` is locally green and you're confident CI will
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
upstream license headers intact, do not edit upstream agent/command `.md`
definitions in place (configure via merged `.opencode/opencode-*.jsonc` instead),
and record which ADOS version a target was installed from so re-runs and updates
are traceable. Details: [`parts/ados-ollama.md`](parts/ados-ollama.md).

### Scratch / Probe Scripts — Explicit Escape Hatch

Throwaway scripts written to investigate ADOS internals or a target repo SHOULD
NOT be held to the standards above. They live in `scratch/` (gitignored),
excluded from lint / format / tests; the hooks SHOULD NOT block on them.
**Promotion**: if a probe script proves repeatedly useful, port it into
`scripts/` or `tools/` with full standards applied — do not let useful logic rot
in `scratch/`. This exception is named explicitly so future contributors don't
"tidy" it away.

## Parts — read only what your task needs

| Work type                              | Read                                                                     |
| -------------------------------------- | ------------------------------------------------------------------------ |
| Bash scripts & PATH-able tools         | [`parts/bash.md`](parts/bash.md)                                         |
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

**Version**: 1.0.0 | **Ratified**: 2026-06-05 | **Last amended**: 2026-06-05
