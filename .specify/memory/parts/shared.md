# Constitution — Shared & cross-cutting part

Authoritative rules for the repo-wide tooling that spans the whole project —
hooks, CI, changelog, and the tool-invocation working-directory rule. Read this
part when your task touches hook/CI config, the changelog, or you need the cwd
rule. Read together with the [constitution](../constitution.md).

## Cross-cutting tooling

- **Hooks**: a shared `.pre-commit-config.yaml` at the repo root runs hooks
  across the repo (shellcheck, shfmt, markdownlint, gitleaks, actionlint,
  gitlint). The runner is `prek` — a fast drop-in replacement for the
  `pre-commit` Python package. Hooks MUST remain active; never bypass with
  `--no-verify` (see the constitution's Quality Gates). (Hooks/CI land with the
  tooling; this records the intended shape.)
- **CI**: GitHub Actions reproduces the local `make check` (shellcheck + `shfmt
  -d` + the bats / embedded test suites) on every pull request.
- **Changelog**: `git-cliff` auto-generates from conventional commits. Do not
  hand-maintain `## [Unreleased]` (regenerate via `git cliff --unreleased`).
  Manual edits are limited to released sections for light curation, factual
  corrections, or formatting cleanup. This is the source-code `CHANGELOG.md` —
  separate from the constitution's own version history in
  [`constitution-maintenance.md`](constitution-maintenance.md).

## Tool-invocation working directory

Prefer the tool's built-in cwd flag over `cd <dir> && <tool>`: `git -C <dir>`,
`make -C <dir>`, `gh -R <owner/repo>`. The `cd … && …` form defeats per-command
Bash allowlists (the leading token becomes `cd`, not the wrapped tool), which
triggers permission prompts that stall AI-agent sessions and adds no clarity for
human readers. Inside a script, use a subshell `(cd "${dir}" && …)` or
`pushd`/`popd` to avoid leaking directory state (see [`bash.md`](bash.md)).

## See also

- [`../constitution.md`](../constitution.md) — Working Rules + discipline.
- [`bash.md`](bash.md), [`ados-ollama.md`](ados-ollama.md) — what the tooling is
  and how it's written.
