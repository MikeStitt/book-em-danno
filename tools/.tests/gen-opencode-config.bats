#!/usr/bin/env bats
# Tests for tools/gen-opencode-config

setup() {
  REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd -P)"
  TOOL="${REPO_ROOT}/tools/gen-opencode-config"
}

@test "gen-opencode-config passes a bash syntax check" {
  bash -n "${TOOL}"
}

@test "gen-opencode-config --help prints usage and exits 0" {
  run "${TOOL}" --help
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"Usage:"* ]]
}

@test "gen-opencode-config --version prints name and version" {
  run "${TOOL}" --version
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"gen-opencode-config"* ]]
}

@test "gen-opencode-config rejects unknown options" {
  run "${TOOL}" --bogus
  [ "${status}" -eq 2 ]
}

@test "generates config with provider, default_agent, and a local + cloud agent" {
  tmp="$(mktemp -d)"
  run "${TOOL}" --target "${tmp}" --local-model gemma3:27b --cloud-model anthropic/claude-sonnet-4-6
  [ "${status}" -eq 0 ]
  cfg="${tmp}/.opencode/opencode.jsonc"
  [ -f "${cfg}" ]
  grep -q '"default_agent": "pm"' "${cfg}"
  grep -q '"ollama"' "${cfg}"
  grep -q '"committer": { "model": "ollama/gemma3:27b" }' "${cfg}"
  grep -q '"architect": { "model": "anthropic/claude-sonnet-4-6" }' "${cfg}"
  rm -rf "${tmp}"
}

@test "generated config is valid JSON once comments are stripped" {
  command -v python3 >/dev/null || skip "python3 not available"
  tmp="$(mktemp -d)"
  run "${TOOL}" --target "${tmp}" --local-model gemma3:27b
  [ "${status}" -eq 0 ]
  # Strip only full-line // comments (preserve any // inside strings).
  sed -E 's:^[[:space:]]*//.*$::' "${tmp}/.opencode/opencode.jsonc" \
    | python3 -m json.tool >/dev/null
  rm -rf "${tmp}"
}

@test "regeneration is idempotent (unchanged on second run)" {
  tmp="$(mktemp -d)"
  "${TOOL}" --target "${tmp}" --local-model gemma3:27b >/dev/null
  run "${TOOL}" --target "${tmp}" --local-model gemma3:27b
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"unchanged"* ]]
  rm -rf "${tmp}"
}

@test "--cloud-only puts every agent on the cloud model" {
  tmp="$(mktemp -d)"
  run "${TOOL}" --target "${tmp}" --local-model gemma4:26b-mlx \
    --cloud-model anthropic/claude-opus-4-8 --cloud-only
  [ "${status}" -eq 0 ]
  cfg="${tmp}/.opencode/opencode.jsonc"
  grep -q '"committer": { "model": "anthropic/claude-opus-4-8" }' "${cfg}"
  ! grep -q 'ollama/' "${cfg}"
  rm -rf "${tmp}"
}

@test "--local-only puts every agent on the local model" {
  tmp="$(mktemp -d)"
  run "${TOOL}" --target "${tmp}" --local-model gemma4:26b-mlx --local-only
  [ "${status}" -eq 0 ]
  cfg="${tmp}/.opencode/opencode.jsonc"
  grep -q '"architect": { "model": "ollama/gemma4:26b-mlx" }' "${cfg}"
  ! grep -qE '"model": "anthropic/' "${cfg}"
  rm -rf "${tmp}"
}

@test "config sets a top-level model and a literal Ollama baseURL" {
  tmp="$(mktemp -d)"
  run "${TOOL}" --target "${tmp}" --local-model gemma4:26b-mlx \
    --cloud-model anthropic/claude-opus-4-8
  [ "${status}" -eq 0 ]
  cfg="${tmp}/.opencode/opencode.jsonc"
  grep -q '"model": "anthropic/claude-opus-4-8"' "${cfg}"
  grep -q '"small_model": "ollama/gemma4:26b-mlx"' "${cfg}"
  grep -q '"baseURL": "http://host.docker.internal:11434/v1"' "${cfg}"
  rm -rf "${tmp}"
}

@test "--local-only sets the top-level model to the local model" {
  tmp="$(mktemp -d)"
  run "${TOOL}" --target "${tmp}" --local-model gemma4:26b-mlx --local-only
  [ "${status}" -eq 0 ]
  grep -q '"model": "ollama/gemma4:26b-mlx"' "${tmp}/.opencode/opencode.jsonc"
  rm -rf "${tmp}"
}

@test "an existing differing config is preserved without --force" {
  tmp="$(mktemp -d)"
  "${TOOL}" --target "${tmp}" --local-model gemma3:27b >/dev/null
  run "${TOOL}" --target "${tmp}" --local-model gemma3:4b
  [ "${status}" -eq 0 ]
  [[ "${output}" == *"preserving"* ]]
  grep -q 'gemma3:27b' "${tmp}/.opencode/opencode.jsonc"
  rm -rf "${tmp}"
}
