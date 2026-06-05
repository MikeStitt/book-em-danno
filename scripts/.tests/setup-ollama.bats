#!/usr/bin/env bats
# Smoke tests for scripts/setup-ollama.sh (no model download)

setup() {
  REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd -P)"
  SCRIPT="${REPO_ROOT}/scripts/setup-ollama.sh"
}

@test "setup-ollama.sh passes a bash syntax check" {
  bash -n "${SCRIPT}"
}

@test "setup-ollama.sh --help prints usage and exits 0" {
  run "${SCRIPT}" --help
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"Usage:"* ]]
}

@test "setup-ollama.sh --version prints name and version" {
  run "${SCRIPT}" --version
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"setup-ollama"* ]]
}

@test "setup-ollama.sh rejects unknown options" {
  run "${SCRIPT}" --bogus
  [ "${status}" -eq 2 ]
}

@test "setup-ollama.sh --dry-run previews without side effects" {
  run "${SCRIPT}" --dry-run --model gemma3:1b
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"DRY-RUN"* ]]
}
