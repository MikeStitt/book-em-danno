# How to release `danno` (Lane B — automated)

Releases are **bot-driven**. You never edit the version by hand, never write the
changelog, and never push a tag. Two workflows do the ceremony; your job is two
clicks.

## TL;DR — cutting a release

1. **Land the work you want to release on `main` first** — via normal PR(s), as
   usual. **Do not** bump the `version` in `pyproject.toml` or edit `CHANGELOG.md`
   in those PRs: the release step does both automatically, and a manual bump
   collides with it. Just merge your feature/fix commits.
2. Go to the **Actions** tab (upstream:
   [MikeStitt/book-em-danno](https://github.com/MikeStitt/book-em-danno/actions);
   **on a fork that link won't apply** — use your own copy at
   `https://github.com/<your-org>/<your-repo>/actions`) and open **`release-prepare`
   → "Run workflow"**. In the **"Use workflow from"** branch dropdown, **keep
   `main`** (it defaults there) — release off `main`, never off a feature branch.
   Leave the version input blank to auto-compute it from your conventional commits,
   or type an override.
3. The bot opens a **`chore(release): vX.Y.Z`** PR that bumps `pyproject.toml`
   and regenerates `CHANGELOG.md`. Review it, let CI go green, and **Merge** it.

That's it. Merging the PR triggers publishing — a `vX.Y.Z` tag and a GitHub
Release (notes from `git-cliff`) appear within a minute. **Do not** run
`gh release create` or push a tag yourself; doing so collides with the workflow
(that is the `422 tag_name already exists` failure this process replaces).

## How it works

```
  you: Run workflow ──▶ release-prepare.yml ──▶ chore(release): vX.Y.Z PR
                                                        │
                                              you: Merge │
                                                        ▼
                          release.yml (on push to main): version in
                          pyproject has no tag yet? ──▶ tag + GitHub Release
```

- **`release-prepare.yml`** (`workflow_dispatch`): `git cliff --bumped-version`
  computes the next semver from commit types (`feat` → minor, `fix` → patch,
  `!`/`BREAKING CHANGE` → major). It bumps `pyproject.toml`, runs
  `git cliff --tag vX.Y.Z` to render the changelog, and opens the PR. No tag is
  created here.
- **`release.yml`** (`on: push: branches: [main]`): every merge to `main` checks
  whether the version in `pyproject.toml` already has a matching `vX.Y.Z` tag.
  Ordinary merges → no-op. The release-PR merge → it creates the tag **and**
  publishes the Release in one job. The check is idempotent: re-running it on an
  already-released version does nothing.

## Prerequisites (one-time setup)

These make the automation work against the protected `main` and (optionally)
locked tags:

1. **`RELEASE_TOKEN` secret** — a PAT or GitHub App token with **contents** +
   **pull-requests** write. `release-prepare` uses it to push the branch and open
   the PR. This is required because the default `GITHUB_TOKEN` cannot raise events
   that trigger other workflows, so `check.yml` would never run on the release PR
   and the `protect-main` required-status-checks rule could never be satisfied.
   Without it, `release-prepare` fails loud.
2. **Tag-ruleset bypass (only if you lock tags)** — if you add a ruleset that
   protects `refs/tags/v*`, list `github-actions[bot]` (the identity `release.yml`
   runs as) as a **bypass actor**, or it cannot push the tag. `release.yml` keeps
   tag creation and publishing in the *same* job precisely so it does not depend
   on a tag-push event cascading to a second workflow (which `GITHUB_TOKEN` would
   not do).

## Notes / caveats

- **Source of truth:** conventional commits. The version and changelog are both
  derived from them, so write good commit messages (see the constitution's
  "Conventional Commits").
- **Override:** pass a `version` input to `release-prepare` for an intentional
  jump (e.g. forcing `1.0.0`) instead of the computed bump.
- **Existing tag drift:** the repo's older tags (e.g. `v0.2.3`) were created
  out-of-band on commits not on `main`, so a local `git cliff --bumped-version`
  may look off until history settles. On CI with full tags fetched the computed
  bump is correct; going forward, all tags come from this flow and stay aligned.
- **Constitution fit:** the version-bump commit still lands on `main` only via a
  reviewed PR (the `protect-main` ruleset's `pull_request` rule), so "never commit
  directly to `main`" holds. Tags are unprotected today; this flow is what you
  use if/when you lock them.
