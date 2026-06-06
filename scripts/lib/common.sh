#!/usr/bin/env bash
# common.sh — shared helpers for the ADOS-on-Ollama toolkit.
# SOURCED, not executed. A caller should set LOG_TAG before or after sourcing.
# shellcheck shell=bash

# Guard against double-sourcing.
[[ -n "${_ADOS_COMMON_SOURCED:-}" ]] && return 0
_ADOS_COMMON_SOURCED=1

: "${LOG_TAG:=(ados)}"
: "${DRY_RUN:=false}"
: "${VERBOSE:=false}"

# Colors (disabled when stdout is not a terminal).
# shellcheck disable=SC2034  # consumed by sourcing scripts
if [[ -t 1 ]]; then
  _C_RED=$'\033[0;31m'
  _C_GRN=$'\033[0;32m'
  _C_YEL=$'\033[0;33m'
  _C_RST=$'\033[0m'
else
  _C_RED="" _C_GRN="" _C_YEL="" _C_RST=""
fi

log_info() { printf '[INFO]  %s %s\n' "${LOG_TAG}" "$*"; }
log_warn() { printf '[WARN]  %s %s\n' "${LOG_TAG}" "$*" >&2; }
log_err() { printf '[ERROR] %s %s\n' "${LOG_TAG}" "$*" >&2; }
log_debug() {
  [[ "${VERBOSE}" == "true" ]] && printf '[DEBUG] %s %s\n' "${LOG_TAG}" "$*"
  return 0
}

die() {
  log_err "$@"
  exit "${EXIT_USAGE:-2}"
}

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }

# require_bash <major> — fail loudly if the running bash is older than <major>.
require_bash() {
  local want="$1"
  if [[ "${BASH_VERSINFO[0]}" -lt "${want}" ]]; then
    die "bash >= ${want} required, but running ${BASH_VERSION}." \
      "Run scripts/setup-dev.sh and put \"\$(brew --prefix)/bin\" on PATH."
  fi
}

# run_cmd — execute, or just log the intent when DRY_RUN=true.
run_cmd() {
  if [[ "${DRY_RUN}" == "true" ]]; then
    # Join args with spaces regardless of the caller's IFS (which is often \n\t).
    local IFS=' '
    log_info "[DRY-RUN] Would execute: $*"
    return 0
  fi
  "$@"
}
