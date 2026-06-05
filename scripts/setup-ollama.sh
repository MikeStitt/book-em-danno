#!/usr/bin/env bash
# setup-ollama.sh — ensure Ollama is running and a Gemma model is available,
# then verify the model responds (and, optionally, that it can tool-call).
#
# Usage: setup-ollama.sh [options]
#   --model <tag>     Gemma model to ensure (default: ${GEMMA_MODEL})
#   --num-ctx <n>     Context window used for verification (default: ${NUM_CTX})
#   --verify-tools    Also probe tool-calling (best-effort; WARNs if unsupported)
#   --no-start        Do not try to start a local Ollama server
#   -f, --force       Re-pull the model even if already present
#   -n, --dry-run     Show what would be done without doing it
#   -v, --verbose     Debug output
#   -h, --help        Show this help
#   -V, --version     Show version
#
# Env: OLLAMA_HOST_URL (default http://localhost:11434), GEMMA_MODEL, NUM_CTX,
#      DRY_RUN, VERBOSE.
# Exit: 0 ok; 2 usage; 4 runtime; 5 external command failure.
set -Eeuo pipefail
set -o errtrace
shopt -s inherit_errexit 2>/dev/null || true
IFS=$'\n\t'

readonly APP_NAME="setup-ollama"
readonly APP_VERSION="0.1.0"
LOG_TAG="(${APP_NAME})"
readonly EXIT_USAGE=2
readonly EXIT_RUNTIME=4
readonly EXIT_EXTERNAL=5

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

require_bash 4

GEMMA_MODEL="${GEMMA_MODEL:-gemma3}"
NUM_CTX="${NUM_CTX:-32000}"
OLLAMA_HOST_URL="${OLLAMA_HOST_URL:-http://localhost:11434}"
START_OLLAMA=true
VERIFY_TOOLS=false
FORCE=false

die_runtime() {
  log_err "$@"
  exit "${EXIT_RUNTIME}"
}
die_external() {
  log_err "$@"
  exit "${EXIT_EXTERNAL}"
}

_ollama_reachable() { curl -fsS "${OLLAMA_HOST_URL}/api/tags" >/dev/null 2>&1; }

ensure_server() {
  if _ollama_reachable; then
    log_info "Ollama reachable at ${OLLAMA_HOST_URL}"
    return 0
  fi
  if [[ "${DRY_RUN}" == "true" ]]; then
    log_info "[DRY-RUN] Would start 'ollama serve' and wait for ${OLLAMA_HOST_URL}"
    return 0
  fi
  if [[ "${START_OLLAMA}" != "true" ]]; then
    die_runtime "Ollama not reachable at ${OLLAMA_HOST_URL} (--no-start). Start it: ollama serve"
  fi
  log_info "Starting Ollama server in the background..."
  nohup ollama serve >/dev/null 2>&1 &
  for _ in $(seq 1 30); do
    if _ollama_reachable; then
      log_info "Ollama is up"
      return 0
    fi
    sleep 1
  done
  die_runtime "Ollama did not become reachable after 30s. Try: ollama serve"
}

model_present() {
  ollama list 2>/dev/null | awk 'NR>1{print $1}' \
    | grep -qiE "^${GEMMA_MODEL%%:*}(:|$)"
}

pull_model() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    log_info "[DRY-RUN] Would ensure model present: ${GEMMA_MODEL}"
    return 0
  fi
  if model_present && [[ "${FORCE}" != "true" ]]; then
    log_info "Model already present: ${GEMMA_MODEL}"
    return 0
  fi
  log_info "Pulling model: ${GEMMA_MODEL} (this may take a while)"
  ollama pull "${GEMMA_MODEL}" || die_external "Failed to pull ${GEMMA_MODEL}"
}

verify_responds() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    log_info "[DRY-RUN] Would verify ${GEMMA_MODEL} responds via ${OLLAMA_HOST_URL}/api/generate"
    return 0
  fi
  log_info "Verifying ${GEMMA_MODEL} responds (num_ctx=${NUM_CTX})..."
  local payload resp
  payload="$(printf '{"model":"%s","prompt":"reply with the single word: ok","stream":false,"options":{"num_ctx":%s}}' \
    "${GEMMA_MODEL}" "${NUM_CTX}")"
  resp="$(curl -fsS "${OLLAMA_HOST_URL}/api/generate" -d "${payload}")" \
    || die_external "Generate request failed for ${GEMMA_MODEL}"
  if [[ "${resp}" == *'"response"'* ]]; then
    log_info "Model responded OK"
  else
    die_external "Unexpected response from ${GEMMA_MODEL}: ${resp}"
  fi
}

verify_tools() {
  [[ "${VERIFY_TOOLS}" == "true" ]] || return 0
  if [[ "${DRY_RUN}" == "true" ]]; then
    log_info "[DRY-RUN] Would probe tool-calling on ${GEMMA_MODEL}"
    return 0
  fi
  log_info "Probing tool-calling on ${GEMMA_MODEL} (best-effort)..."
  local payload resp
  payload="$(printf '{"model":"%s","stream":false,"messages":[{"role":"user","content":"What is the weather in Paris? Use the tool."}],"tools":[{"type":"function","function":{"name":"get_weather","description":"Get the weather for a city","parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}],"options":{"num_ctx":%s}}' \
    "${GEMMA_MODEL}" "${NUM_CTX}")"
  resp="$(curl -fsS "${OLLAMA_HOST_URL}/api/chat" -d "${payload}" 2>/dev/null || true)"
  if [[ "${resp}" == *'"tool_calls"'* ]]; then
    log_info "Tool-calling works on ${GEMMA_MODEL}"
  else
    log_warn "${GEMMA_MODEL} did not emit a tool call. Tool-heavy local agents"
    log_warn "(runner/committer) may be unreliable on it — consider a larger/"
    log_warn "tool-capable model or keep those agents on cloud in the hybrid map."
  fi
}

usage() {
  cat <<EOF
Usage: ${APP_NAME} [options]

Ensure Ollama is running and a Gemma model is available, then verify it
responds. Idempotent: an already-present model is not re-pulled.

Options:
  --model <tag>     Gemma model to ensure (default: ${GEMMA_MODEL})
  --num-ctx <n>     Context window for verification (default: ${NUM_CTX})
  --verify-tools    Also probe tool-calling (best-effort; WARNs if unsupported)
  --no-start        Do not try to start a local Ollama server
  -f, --force       Re-pull the model even if present
  -n, --dry-run     Show what would be done without doing it
  -v, --verbose     Debug output
  -h, --help        Show this help
  -V, --version     Show version
EOF
}

main() {
  while (($#)); do
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      -V | --version)
        printf '%s %s\n' "${APP_NAME}" "${APP_VERSION}"
        exit 0
        ;;
      --model)
        shift
        GEMMA_MODEL="${1:?--model requires a tag}"
        ;;
      --num-ctx)
        shift
        NUM_CTX="${1:?--num-ctx requires a value}"
        ;;
      --verify-tools) VERIFY_TOOLS=true ;;
      --no-start) START_OLLAMA=false ;;
      -f | --force) FORCE=true ;;
      -n | --dry-run) DRY_RUN=true ;;
      -v | --verbose) VERBOSE=true ;;
      *) die "Unknown option: $1" ;;
    esac
    shift
  done

  if [[ "${DRY_RUN}" != "true" ]]; then
    require_cmd ollama
    require_cmd curl
  fi

  ensure_server
  pull_model
  verify_responds
  verify_tools

  log_info "Done. ${GEMMA_MODEL} is ready at ${OLLAMA_HOST_URL}."
}

if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
