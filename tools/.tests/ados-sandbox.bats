#!/usr/bin/env bats
# Tests for tools/ados-sandbox (command construction only; no real VM launch)

setup() {
  REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd -P)"
  TOOL="${REPO_ROOT}/tools/ados-sandbox"
}

@test "ados-sandbox passes a bash syntax check" {
  bash -n "${TOOL}"
}

@test "ados-sandbox --help prints usage and exits 0" {
  run "${TOOL}" --help
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"Usage:"* ]]
}

@test "ados-sandbox --version prints name and version" {
  run "${TOOL}" --version
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"ados-sandbox"* ]]
}

@test "ados-sandbox rejects unknown options" {
  run "${TOOL}" --bogus
  [ "${status}" -eq 2 ]
}

@test "ados-sandbox --dry-run prints the docker sandbox workflow" {
  # Resolve symlinks (/var -> /private/var on macOS) so the assertion matches TARGET_ABS.
  tmp="$(cd "$(mktemp -d)" && pwd -P)"
  run "${TOOL}" --target "${tmp}" --name probe --dry-run
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"docker sandbox create --name probe opencode ${tmp}"* ]]
  [[ "${output}" == *"docker sandbox network proxy probe --allow-host localhost:11434"* ]]
  [[ "${output}" == *"host.docker.internal"* ]]
  [[ "${output}" == *"opencode"* ]]
  rm -rf "${tmp}"
}

@test "ados-sandbox warns when target lacks an opencode config" {
  tmp="$(mktemp -d)"
  run "${TOOL}" --target "${tmp}" --name probe --dry-run
  [[ "${output}" == *"run gen-opencode-config first"* ]]
  rm -rf "${tmp}"
}
