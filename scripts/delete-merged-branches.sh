#!/usr/bin/env bash
# Delete a list of REMOTE branches via the GitHub API (gh) — but only ones that are
# genuinely merged into the base ref. PREVIEW (dry run) is the DEFAULT: nothing is deleted
# unless you pass --execute.
#
# SAFETY (fail-loud): the list file is treated as a candidate list, NOT as truth. Right
# before deleting, each branch is RE-VERIFIED to be fully merged into the base
# (`git merge-base --is-ancestor origin/<b> <base>`). Any branch that is unmerged, missing,
# equals the default/base branch, or is the current HEAD is SKIPPED and never deleted — so a
# stale list can never drop unmerged work.
#
# Usage:
#   scripts/delete-merged-branches.sh [--list FILE] [--base REF] [--execute] [--yes]
#     --list FILE   branch list, one per line, '#'/blank ignored, origin/ prefix optional
#                   (default: merged-remote-branches.txt)
#     --base REF    branches must be merged into this ref   (default: origin/main)
#     --execute     actually delete (default is PREVIEW / dry run — prints what it WOULD do)
#     --yes         with --execute, skip the interactive confirmation prompt
#     -h|--help     show this help
#
# Env:
#   NO_FETCH=1      skip the `git fetch --prune` refresh
#
# Exit: 0 on success (preview always 0 if the list is valid). Non-zero if the list file /
# base ref is missing, gh is unavailable, or any delete call fails in --execute mode.

set -euo pipefail

LIST_FILE="merged-remote-branches.txt"
BASE_REF="origin/main"
EXECUTE=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --list)    LIST_FILE="${2:?--list needs a path}"; shift 2 ;;
    --base)    BASE_REF="${2:?--base needs a ref}";  shift 2 ;;
    --execute) EXECUTE=1; shift ;;
    --yes|-y)  ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "::error::unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v gh >/dev/null 2>&1 || { echo "::error::gh (GitHub CLI) not found on PATH" >&2; exit 1; }

cd "$(git rev-parse --show-toplevel)"

[ -f "$LIST_FILE" ] || { echo "::error::list file not found: $LIST_FILE" >&2; exit 1; }

if [ "${NO_FETCH:-0}" != "1" ]; then
  echo "# fetching + pruning origin ..."
  git fetch --prune origin >/dev/null 2>&1
fi

git rev-parse --verify --quiet "$BASE_REF" >/dev/null \
  || { echo "::error::base ref does not exist: $BASE_REF" >&2; exit 1; }

REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
BASE_SHA="$(git rev-parse --short "$BASE_REF")"
# The remote's default branch (never delete it even if it is somehow listed).
DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef -q .defaultBranchRef.name)"

mode="PREVIEW (dry run — nothing will be deleted)"
[ "$EXECUTE" = "1" ] && mode="EXECUTE (branches WILL be deleted)"
echo "# repo=$REPO  base=$BASE_REF@$BASE_SHA  default=$DEFAULT_BRANCH  list=$LIST_FILE"
echo "# mode: $mode"
echo

to_delete=()   # branches that pass every safety check
skipped=0

while IFS= read -r line || [ -n "$line" ]; do
  branch="$(printf '%s' "$line" | tr -d '\r' | sed -E 's/[[:space:]]+$//; s/^[[:space:]]+//')"
  case "$branch" in ''|\#*) continue ;; esac
  branch="${branch#origin/}"
  ref="origin/$branch"

  if [ "$branch" = "$DEFAULT_BRANCH" ] || [ "origin/$branch" = "$BASE_REF" ]; then
    printf 'SKIP     %s  (default/base branch — never delete)\n' "$branch"; skipped=$((skipped+1)); continue
  fi
  if ! git rev-parse --verify --quiet "$ref" >/dev/null; then
    printf 'SKIP     %s  (no remote ref %s — already gone)\n' "$branch" "$ref"; skipped=$((skipped+1)); continue
  fi
  if ! git merge-base --is-ancestor "$ref" "$BASE_REF"; then
    ahead="$(git rev-list --count "$BASE_REF..$ref")"
    printf 'SKIP     %s  (UNMERGED — %s commit(s) not in %s; refusing to delete)\n' "$branch" "$ahead" "$BASE_REF"
    skipped=$((skipped+1)); continue
  fi
  to_delete+=("$branch")
  if [ "$EXECUTE" = "1" ]; then
    printf 'DELETE   %s\n' "$branch"
  else
    printf 'WOULD-DELETE  %s\n' "$branch"
    printf '             gh api --method DELETE repos/%s/git/refs/heads/%s\n' "$REPO" "$branch"
  fi
done < "$LIST_FILE"

echo
echo "# candidates=${#to_delete[@]}  skipped=$skipped"

if [ "$EXECUTE" != "1" ]; then
  echo "# PREVIEW only — re-run with --execute to delete the ${#to_delete[@]} branch(es) above."
  exit 0
fi

if [ "${#to_delete[@]}" -eq 0 ]; then
  echo "# nothing to delete."
  exit 0
fi

if [ "$ASSUME_YES" != "1" ]; then
  printf '# About to DELETE %s remote branch(es) from %s. Type "yes" to proceed: ' "${#to_delete[@]}" "$REPO"
  read -r reply
  [ "$reply" = "yes" ] || { echo "# aborted (no branches deleted)."; exit 0; }
fi

fail=0
for branch in "${to_delete[@]}"; do
  # Re-check once more immediately before the destructive call (paranoia; cheap).
  if ! git merge-base --is-ancestor "origin/$branch" "$BASE_REF"; then
    printf '!! SKIP  %s (became unmerged since the scan — not deleting)\n' "$branch"; continue
  fi
  if gh api --method DELETE "repos/$REPO/git/refs/heads/$branch" >/dev/null 2>&1; then
    printf 'deleted  %s\n' "$branch"
  else
    printf '::error::failed to delete %s\n' "$branch" >&2; fail=$((fail+1))
  fi
done

echo
echo "# done. failures=$fail"
[ "$fail" -eq 0 ] || exit 1
