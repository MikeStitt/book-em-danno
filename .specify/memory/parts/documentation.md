# Documentation Organization — Developer-Facing Docs

This part defines where to put different kinds of documentation and how to
keep them organized.

## Directory Purpose

### `docs/` — User-facing design docs

Stories, architectural decisions, and deep dives that help developers understand
the project's design rationale. These are **publication-ready** docs:

- Feature explanations (e.g., `danno.workspace.toml-explanation.md`)
- Architecture decision records
- Design patterns used in the codebase

**Hygiene:** markdownlint + Prettier apply; no working notes.

### `.docs/` — Dev docs, working notes, internal artifacts

Developer-facing working notes, investigation summaries, plan artifacts.
Exempt from document-quality checks (see shared.md:20–26).

Examples:

- Investigation findings (`danno.workspace.toml-finding-summary.md`)
- Session notes and plan fragments
- Drafts awaiting migration to `docs/`

**Note:** This directory is **exempt** from markdownlint/Prettier per
`.markdownlintignore` and `.prettierignore`, with matching
`exclude: ^\.docs/` in `.pre-commit-config.yaml`.

### `.specify/memory/parts/` — Constitution parts

Authoritative rules for development practices (see
constitution.md:192–201).

## Cross-References

- **Constitution Documentation Hygiene**: constitution.md:149–154
  - Behavior-affecting changes MUST update docs in same commit
- **Shared .docsExemption**: shared.md:20–26
  - `.docs/` exempt from markdownlint/Prettier; hooks configured

## Rules of Thumb

1. **User-facing, polished?** → `docs/`
2. **Investigation working notes, draft?** → `.docs/`
3. **Development practice rules?** → `.specify/memory/parts/`
