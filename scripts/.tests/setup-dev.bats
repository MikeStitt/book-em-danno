#!/usr/bin/env bats
# Smoke tests for scripts/setup-dev.sh

setup() {
  REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd -P)"
  SCRIPT="${REPO_ROOT}/scripts/setup-dev.sh"
}

@test "setup-dev.sh passes a bash syntax check" {
  bash -n "${SCRIPT}"
}

@test "setup-dev.sh --help prints usage and exits 0" {
  run "${SCRIPT}" --help
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"Usage:"* ]]
}

@test "setup-dev.sh rejects unknown options" {
  run "${SCRIPT}" --bogus
  [ "${status}" -eq 2 ]
}

@test "setup-dev.sh --dry-run installs nothing" {
  run "${SCRIPT}" --dry-run
  # With Homebrew present this exits 0 and only logs skip/DRY-RUN lines;
  # it must never emit brew's real install banner.
  [[ "${output}" != *"==> Installing"* ]]
}
