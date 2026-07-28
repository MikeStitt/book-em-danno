#!/usr/bin/env bash
# Re-verify that every branch listed in a merged-branches file is FULLY merged into main.
#
# "Fully merged" == the branch tip is an ancestor of origin/main, i.e. every commit on the
# branch is already contained in main (so the branch is safe to delete). This is the same
# test `git branch --merged` uses, applied to remote-tracking branches.
#
# Usage:
#   scripts/verify-merged-branches.sh [LIST_FILE] [BASE_REF]
#     LIST_FILE  branch list, one name per line, '#'/blank lines ignored,
#                names may include or omit a leading "origin/" (default: merged-remote-branches.txt)
#     BASE_REF   the ref everything must be merged into        (default: origin/main)
#
# Env:
#   NO_FETCH=1   skip the `git fetch --prune` refresh (use the local remote-tracking refs as-is)
#
# Exit status: 0 if ALL listed branches are merged; 1 if any is unmerged, missing, or the
# list file is absent. Prints a PASS/FAIL line per branch and a summary.

set -euo pipefail

LIST_FILE="${1:-merged-remote-branches.txt}"
BASE_REF="${2:-origin/main}"

# Run from the repo root regardless of the caller's cwd.
cd "$(git rev-parse --show-toplevel)"

if [ ! -f "$LIST_FILE" ]; then
  echo "::error::list file not found: $LIST_FILE" >&2
  exit 1
fi

if [ "${NO_FETCH:-0}" != "1" ]; then
  echo "# fetching + pruning origin ..."
  git fetch --prune origin >/dev/null 2>&1
fi

if ! git rev-parse --verify --quiet "$BASE_REF" >/dev/null; then
  echo "::error::base ref does not exist: $BASE_REF" >&2
  exit 1
fi

base_sha="$(git rev-parse --short "$BASE_REF")"
echo "# verifying all branches in '$LIST_FILE' are merged into $BASE_REF ($base_sha)"
echo

merged=0 unmerged=0 missing=0

while IFS= read -r line || [ -n "$line" ]; do
  # strip inline whitespace / CR, skip comments and blanks
  branch="$(printf '%s' "$line" | tr -d '\r' | sed -E 's/[[:space:]]+$//; s/^[[:space:]]+//')"
  case "$branch" in
    ''|\#*) continue ;;
  esac

  # accept names with or without the origin/ prefix; test the remote-tracking ref
  ref="origin/${branch#origin/}"

  if ! git rev-parse --verify --quiet "$ref" >/dev/null; then
    printf 'MISSING  %s  (no remote-tracking ref %s — deleted on remote or never pushed)\n' "$branch" "$ref"
    missing=$((missing + 1))
    continue
  fi

  if git merge-base --is-ancestor "$ref" "$BASE_REF"; then
    printf 'MERGED   %s\n' "$branch"
    merged=$((merged + 1))
  else
    ahead="$(git rev-list --count "$BASE_REF..$ref")"
    printf 'UNMERGED %s  (%s commit(s) not in %s)\n' "$branch" "$ahead" "$BASE_REF"
    unmerged=$((unmerged + 1))
  fi
done < "$LIST_FILE"

echo
echo "# summary: merged=$merged unmerged=$unmerged missing=$missing (base=$BASE_REF@$base_sha)"

if [ "$unmerged" -ne 0 ] || [ "$missing" -ne 0 ]; then
  echo "::error::not all listed branches are merged into $BASE_REF (unmerged=$unmerged missing=$missing)" >&2
  exit 1
fi
echo "OK — all $merged listed branches are fully merged into $BASE_REF."
