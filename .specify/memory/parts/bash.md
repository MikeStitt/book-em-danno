# Constitution — Bash part

Authoritative standards for the Bash tooling that is this repository's primary
deliverable. Read this part when writing or editing anything under `scripts/` or
`tools/`. Read together with the [constitution](../constitution.md) and
[`testing.md`](testing.md).

The detailed, line-level standard is ADOS's own
[`.ai/rules/bash.md`](https://github.com/juliusz-cwiakalski/agentic-delivery-os/blob/main/.ai/rules/bash.md)
(MIT) — it is the authoritative reference and this part deliberately does not
duplicate it. Once ADOS is installed locally that file is available at
`../agentic-delivery-os/.ai/rules/bash.md`. The essentials below are what every
script in this repo MUST satisfy.

## Stack

- **Shell**: Bash 4.0+ (associative arrays, `mapfile`). macOS ships Bash 3.2, so
  do not assume the system `/bin/bash`; use `#!/usr/bin/env bash` and document a
  Bash 4+ requirement in the header.
- **Lint**: `shellcheck` (config in `.shellcheckrc`, `enable=all`).
- **Format**: `shfmt -i 2 -ci -bn` (check with `shfmt -d` in `make check`).
- **Test**: the embedded test framework from ADOS's bash rules (no external
  dependency) and/or `bats`; see [`testing.md`](testing.md).

## Layout (mirrors ADOS)

- `scripts/` — repo-internal automation, **`.sh` extension**, kebab-case
  (`install.sh`, `add-header-location.sh`).
- `tools/` — PATH-able CLI utilities, **no extension** (so they read as commands
  once on `PATH`).
- Tests live in a `.tests/` subfolder adjacent to what they test
  (`scripts/.tests/`, `tools/.tests/`), named `test-*.sh` and executable. A
  top-level `scripts/test-all.sh` aggregator discovers and runs them.

## Mandatory per-script rules

- Shebang `#!/usr/bin/env bash`; then `set -Eeuo pipefail`, `set -o errtrace`,
  `shopt -s inherit_errexit 2>/dev/null || true`, `IFS=$'\n\t'`.
- `trap` handlers for `ERR`, `EXIT`, `INT`/`TERM` (centralized error + cleanup).
- Quote every expansion (`"${var}"`, `"${arr[@]}"`); prefer `[[ … ]]` and
  `printf` over `[ … ]` and `echo`.
- Support `-h|--help` and `--version`; validate inputs early; fail fast with
  actionable messages on stderr; use documented exit codes.
- Leveled logging (`log_info`/`log_warn`/`log_err`/`log_debug`) with a stable
  per-script context tag so CI logs are scannable.
- External commands wrapped in mockable functions and checked with
  `command -v` / `require_cmd`; temp files via `mktemp`, cleaned in the trap.
- **Never** `rm -rf "${var}"` without first validating `${var}` is non-empty,
  not `/`, and the expected kind of path.
- Testable main guard: `if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then main "$@"; fi`.

## Design for this repo's job

Because the tooling installs/adjusts ADOS into **other** repositories
(constitution Working Rule 6):

- **`--dry-run` is mandatory** for any script with side effects. Route mutating
  operations through a `run_cmd` wrapper that logs `[DRY-RUN] Would …` instead of
  executing when `DRY_RUN=true`.
- **Idempotent operations** — re-running converges; detect "already installed"
  and no-op rather than re-applying or clobbering.
- **Dependency injection via environment** (e.g. `OLLAMA_CMD`, `OPENCODE_CMD`,
  `ADOS_REPO`, `TARGET_DIR` with sane defaults) so scripts are testable without
  touching the real system.
- **Subshell or `pushd`/`popd`** for directory changes against a target repo —
  never leak `cd` into shell state (see the cwd rule in
  [`shared.md`](shared.md)).

## Checklist before committing a script

Shebang + strict mode + traps · all vars quoted · `-h`/`--version` · inputs
validated · documented exit codes · context-tagged logging · deps checked ·
`mktemp` + trap cleanup · `--dry-run` for side effects · idempotent · testable
main guard · env-injectable · `shellcheck` clean · `shfmt` applied · tests
written and passing.

## See also

- [`../constitution.md`](../constitution.md) — Working Rules + Quality Gates.
- [`ados-ollama.md`](ados-ollama.md) — what the scripts actually install/configure.
- [`testing.md`](testing.md) — the bats / embedded test framework.
