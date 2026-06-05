# Constitution — Testing part

Authoritative testing taxonomy for this repository's Bash tooling. Read this part
when writing or organizing tests, alongside [`bash.md`](bash.md) and the
[constitution](../constitution.md). Tests are a first-class artifact; `make
check` (the constitution's Quality Gate) runs them.

**Test-Driven Development is strongly encouraged.** Write tests before
implementation to ensure requirements are met and code is robust — especially for
the install/adjust flow, where a regression silently corrupts a target repo.

Tests are divided along two axes.

## Purpose

- **Type 1 — Executable documentation**: DAMP (Descriptive And Meaningful
  Phrases), well-commented, placed "above the fold" in test files. Read like
  tutorials.
- **Type 2 — Coverage & reliability**: edge cases, path coverage. Can be DRY but
  must remain readable; never fail opaquely.

## Scope

- **Unit** — pure functions in isolation (no filesystem, network, or external
  commands; mock all dependencies).
- **Integration** — interactions between functions, using temp directories for
  filesystem operations; may call real external commands.
- **Smoke** — quick critical-path checks.
- **Behavior / E2E** — run a script as a user would invoke it (`--help`,
  `--dry-run`, a full install against a throwaway target repo); assert on exit
  codes, stdout, stderr, and file side-effects.
- **Regression** — specific tests added when bugs are found.

The install/adjust flow especially MUST have behavior tests that assert
**idempotency** (run twice → second run is a no-op) and **preservation** (a
project-specific file is left untouched), per constitution Working Rule 6.

## What to test

- Public interfaces (CLI surface, exit-code contract) thoroughly.
- Private helpers only through the public interface.

## Framework & homes

- Tests use the embedded Bash test framework from ADOS's bash rules (no external
  dependency) and/or `bats`.
- Homes: `scripts/.tests/` and `tools/.tests/`, files named `test-*.sh` and
  executable, discovered by the `scripts/test-all.sh` aggregator (which `make
  check` invokes).

## See also

- [`../constitution.md`](../constitution.md) — Working Rules + Quality Gates.
- [`bash.md`](bash.md) — script standards and the testable-main-guard pattern.
