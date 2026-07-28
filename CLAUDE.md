# CLAUDE.md

This file is a **thin pointer**. The authoritative development contract for this
repository is [`.specify/memory/constitution.md`](.specify/memory/constitution.md)
(the "Constitution"). It supersedes any conflicting guidance here — change a rule
**there**, not in this file.

## Mandatory ritual — every session start and every compaction

Whenever you (Claude) **start a new session** in this repository **or are
compacted**, you MUST:

1. **Re-read** [`.specify/memory/constitution.md`](.specify/memory/constitution.md)
   in full (plus any `parts/` file relevant to the task at hand — read only what
   your task needs).
2. **Explicitly re-agree** to follow it, in your first response, with the words:
   **"I agree to follow constitution.md — so help me 'bot."**

This is how the user confirms you have not forgotten the contract after a context
reset. Do not skip it, and do not treat a summarized/compacted context as an
exemption — re-read and re-agree.

## The contract, in one breath

Think before coding; simplicity and surgical changes; read before you write;
non-destructive, idempotent target installs; fail loud (never claim "done" or
"tests pass" when anything was skipped); checkpoint long operations; `ninja check`
green before done; feature branches (never commit to `main`), stack on unmerged
work; Conventional Commits; docs updated in the same commit as the behavior they
describe. See the Constitution for the full, authoritative text.
