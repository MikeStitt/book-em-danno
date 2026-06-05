#!/usr/bin/env bats
# Tests for tools/ados-sandbox-macos

setup() {
  REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd -P)"
  TOOL="${REPO_ROOT}/tools/ados-sandbox-macos"
}

@test "ados-sandbox-macos passes a bash syntax check" {
  bash -n "${TOOL}"
}

@test "ados-sandbox-macos --help prints usage and exits 0" {
  run "${TOOL}" --help
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"Usage:"* ]]
}

@test "ados-sandbox-macos --version prints name and version" {
  run "${TOOL}" --version
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"ados-sandbox-macos"* ]]
}

@test "ados-sandbox-macos rejects unknown options" {
  run "${TOOL}" --bogus
  [ "${status}" -eq 2 ]
}

@test "ados-sandbox-macos --dry-run prints a write-confined profile + command" {
  tmp="$(cd "$(mktemp -d)" && pwd -P)"
  run "${TOOL}" --target "${tmp}" --dry-run -- opencode --foo
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"(deny file-write*)"* ]]
  [[ "${output}" == *"(subpath \"${tmp}\")"* ]]
  [[ "${output}" == *"sandbox-exec -f <profile> opencode --foo"* ]]
  rm -rf "${tmp}"
}

@test "ados-sandbox-macos actually confines writes to the target (macOS)" {
  [[ "$(uname -s)" == "Darwin" ]] || skip "macOS only"
  command -v sandbox-exec >/dev/null || skip "no sandbox-exec"
  tmp="$(cd "$(mktemp -d)" && pwd -P)"
  outside="${HOME}/ados_sbx_nope_$$"
  run "${TOOL}" --target "${tmp}" -- /bin/sh -c \
    "touch '${tmp}/inside' && (touch '${outside}' 2>/dev/null && echo WROTE-OUTSIDE || echo BLOCKED-OUTSIDE)"
  [ "${status}" -eq 0 ]
  [ -f "${tmp}/inside" ]
  [[ "${output}" == *"BLOCKED-OUTSIDE"* ]]
  [ ! -f "${outside}" ]
  rm -f "${outside}" 2>/dev/null || true
  rm -rf "${tmp}"
}
