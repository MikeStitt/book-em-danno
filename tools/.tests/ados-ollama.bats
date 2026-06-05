#!/usr/bin/env bats
# Tests for tools/ados-ollama (orchestrator)

setup() {
  REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd -P)"
  TOOL="${REPO_ROOT}/tools/ados-ollama"
  ADOS="${REPO_ROOT}/../agentic-delivery-os"
}

@test "ados-ollama passes a bash syntax check" {
  bash -n "${TOOL}"
}

@test "ados-ollama --help prints usage and exits 0" {
  run "${TOOL}" --help
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"Commands:"* ]]
}

@test "ados-ollama --version prints name and version" {
  run "${TOOL}" --version
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"ados-ollama"* ]]
}

@test "ados-ollama with no command exits 2" {
  run "${TOOL}"
  [ "${status}" -eq 2 ]
}

@test "ados-ollama rejects unknown commands" {
  run "${TOOL}" bogus
  [ "${status}" -eq 2 ]
}

@test "ados-ollama rejects unknown options" {
  run "${TOOL}" --bogus
  [ "${status}" -eq 2 ]
}

@test "ados-ollama configure --dry-run forwards to gen-opencode-config" {
  tmp="$(mktemp -d)"
  run "${TOOL}" configure --target "${tmp}" --model gemma3:27b --dry-run
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"Would write"* ]]
  [[ "${output}" == *"opencode.jsonc"* ]]
  rm -rf "${tmp}"
}

@test "ados-ollama all --dry-run runs install + setup + configure" {
  [ -d "${ADOS}/.opencode/agent" ] || skip "ADOS checkout not available"
  tmp="$(mktemp -d)"
  git -C "${tmp}" init -q
  run "${TOOL}" all --target "${tmp}" --model gemma3:27b --dry-run
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"install ADOS"* ]]
  [[ "${output}" == *"setup Ollama"* ]]
  [[ "${output}" == *"write opencode config"* ]]
  [[ "${output}" == *"Would write"* ]]
  rm -rf "${tmp}"
}
