#!/usr/bin/env bats
# Tests for scripts/gen-cli-reference.sh

setup() {
  REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd -P)"
  SCRIPT="${REPO_ROOT}/scripts/gen-cli-reference.sh"
  DOC="${REPO_ROOT}/docs/cli-reference.md"
}

@test "gen-cli-reference.sh passes a bash syntax check" {
  bash -n "${SCRIPT}"
}

@test "gen-cli-reference.sh --help prints usage and exits 0" {
  run "${SCRIPT}" --help
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"Usage:"* ]]
}

@test "gen-cli-reference.sh --version prints name and version" {
  run "${SCRIPT}" --version
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"gen-cli-reference"* ]]
}

@test "gen-cli-reference.sh rejects unknown options" {
  run "${SCRIPT}" --bogus
  [ "${status}" -eq 2 ]
}

@test "--dry-run renders the reference without writing" {
  run "${SCRIPT}" --dry-run
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"# CLI reference"* ]]
  [[ "${output}" == *'## `tools/ados-ollama`'* ]]
}

@test "the committed reference is current (run 'make cli-reference' if this fails)" {
  run "${SCRIPT}" --check
  [ "${status}" -eq 0 ]
}

@test "the reference documents every user-facing tool" {
  for t in tools/ados-ollama tools/ados-ollama-doctor scripts/setup-ollama.sh \
    scripts/install-ados.sh tools/gen-opencode-config tools/ados-sandbox \
    tools/ados-sandbox-macos scripts/setup-dev.sh; do
    grep -qF "## \`${t}\`" "${DOC}"
  done
}
