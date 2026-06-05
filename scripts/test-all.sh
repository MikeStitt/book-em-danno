#!/usr/bin/env bash
# test-all.sh — discover and run every bats suite under a .tests/ directory.
# Exit 0 if all pass OR none are found; non-zero if any fail.
#
# Dependencies: bash>=4, bats.
set -Eeuo pipefail
set -o errtrace
shopt -s inherit_errexit 2>/dev/null || true
IFS=$'\n\t'

readonly LOG_TAG="(test-all)"
log_info() { printf '[INFO]  %s %s\n' "${LOG_TAG}" "$*"; }
log_err() { printf '[ERROR] %s %s\n' "${LOG_TAG}" "$*" >&2; }

main() {
  local repo_root
  repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

  command -v bats >/dev/null 2>&1 \
    || {
      log_err "bats not found — run scripts/setup-dev.sh"
      return 5
    }

  local -a test_files=()
  while IFS= read -r -d '' f; do
    test_files+=("${f}")
  done < <(find "${repo_root}" -type d -name .git -prune -o \
    -type f -name '*.bats' -path '*/.tests/*' -print0 | sort -z)

  if [[ "${#test_files[@]}" -eq 0 ]]; then
    log_info "no bats tests found"
    return 0
  fi

  log_info "running ${#test_files[@]} bats file(s)"
  bats "${test_files[@]}"
}

main "$@"
