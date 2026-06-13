# Constitution — Shared & cross-cutting part

Authoritative rules for the repo-wide tooling that spans the whole project —
hooks, CI, changelog, and the tool-invocation working-directory rule. Read this
part when your task touches hook/CI config, the changelog, or you need the cwd
rule. Read together with the [constitution](../constitution.md).

## Cross-cutting tooling

- **Hooks**: a shared `.pre-commit-config.yaml` at the repo root runs the fast
  subset across the repo (`ruff`, `ruff-format --check`, `markdownlint`, plus
  generic hygiene hooks — trailing-whitespace, end-of-file, merge-conflict,
  check-yaml). The runner is `prek` — a fast drop-in replacement for the
  `pre-commit` Python package. Hooks MUST remain active; never bypass with
  `--no-verify` (see the constitution's Quality Gates). The heavier gate (`mypy`,
  `pytest`) runs via `ninja check`, not in pre-commit, to keep the hook fast.
- **CI**: GitHub Actions (`.github/workflows/check.yml`) installs `uv` + `ninja`
  and runs `ninja check` (ruff + ruff-format + mypy + pytest) on **macOS and
  Linux** on every pull request.
- **`.docs/` is exempt from document-quality checks.** The living plan/status
  tracker and working notes under `.docs/` are committed frequently as work
  proceeds, so markdownlint and Prettier MUST skip them — see `.markdownlintignore`
  and `.prettierignore` (both list `.docs/`), and the matching `exclude: ^\.docs/`
  in `.pre-commit-config.yaml`. A noisy git history under `.docs/` is accepted by
  design. This is the same spirit as the scratch escape-hatch: working artefacts
  are not held to publication standards.
- **Changelog**: `git-cliff` auto-generates `CHANGELOG.md` from conventional
  commits, configured by [`cliff.toml`](../../../cliff.toml) at the repo root.
  `git-cliff` is a Rust binary (not a pip dep) → install it once with
  `brew install git-cliff`. Regenerate the unreleased section with
  `git cliff -o CHANGELOG.md`; do not hand-maintain `## [unreleased]`. Manual
  edits are limited to released sections for light curation, factual corrections,
  or formatting cleanup. This is the source-code `CHANGELOG.md` — separate from
  the constitution's own version history in
  [`constitution-maintenance.md`](constitution-maintenance.md).

### Cutting a release

Releases are **bot-driven** — no human edits the version, writes the changelog,
or pushes a tag. Two workflows split the work; your only actions are running one
workflow and merging one PR. The full how-to, prerequisites (the `RELEASE_TOKEN`
secret, optional tag-ruleset bypass), and caveats live in
[`plans/releasing.md`](../../../plans/releasing.md). In brief:

1. **Run [`release-prepare.yml`](../../../.github/workflows/release-prepare.yml)**
   from the Actions tab. `git cliff --bumped-version` computes the next semver
   from the conventional-commit history, bumps `version` in `pyproject.toml` (what
   `danno --version` reports), regenerates `CHANGELOG.md`, and opens a
   `chore(release): vX.Y.Z` PR. No tag is created here.
2. **Merge that PR.** [`release.yml`](../../../.github/workflows/release.yml) runs
   on every push to `main`; when it sees a `pyproject.toml` version with no
   matching `vX.Y.Z` tag (i.e. the release PR just merged) it creates the tag and
   publishes the GitHub Release — notes from `git-cliff` (reusing `cliff.toml`) —
   in one job. Ordinary merges are a no-op.

The version-bump commit still reaches `main` only through a reviewed PR, so the
`protect-main` "no direct commits" rule holds. `cliff.toml`'s
`tag_pattern = "v[0-9]*"` makes `git-cliff` group commits under each `vX.Y.Z`
tag; untagged commits land under `## [unreleased]`. Do **not** run
`gh release create` or push a tag by hand — that collides with the workflow
(`422 tag_name already exists`).

## Tool-invocation working directory

Prefer the tool's built-in cwd flag over `cd <dir> && <tool>`: `git -C <dir>`,
`make -C <dir>`, `gh -R <owner/repo>`. The `cd … && …` form defeats per-command
Bash allowlists (the leading token becomes `cd`, not the wrapped tool), which
triggers permission prompts that stall AI-agent sessions and adds no clarity for
human readers. In Python, pass the directory to the tool (`git -C`, `cwd=` on
`subprocess.run`, or a `subprocess` `cwd` kwarg) rather than `os.chdir`, to avoid
leaking process-wide directory state.

## See also

- [`../constitution.md`](../constitution.md) — Working Rules + discipline.
- [`python.md`](python.md), [`ados-ollama.md`](ados-ollama.md) — what the tooling
  is and how it's written.
