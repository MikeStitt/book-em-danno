#!/usr/bin/env bash
# setup-dev.sh — install the host tools needed to develop this toolkit.
#
# Intentionally bash 3.2-compatible: this is the ONE script that may run under the
# macOS system bash (3.2) BEFORE a modern bash is installed. Keep it simple — no
# associative arrays, no mapfile, no ${var^^}.
#
# Dependencies: brew (Homebrew). Installs: bash (>=5), shellcheck, shfmt, bats-core.
# Usage: ./scripts/setup-dev.sh [-n|--dry-run] [-h|--help]
#
# Exit codes: 0 success, 2 usage/config error.
set -Eeuo pipefail
shopt -s inherit_errexit 2>/dev/null || true
IFS=$'\n\t'

readonly APP_NAME="setup-dev"
readonly LOG_TAG="(${APP_NAME})"
DRY_RUN="${DRY_RUN:-false}"

BREW_PKGS=(bash shellcheck shfmt bats-core)

log_info() { printf '[INFO]  %s %s\n' "${LOG_TAG}" "$*"; }
log_err() { printf '[ERROR] %s %s\n' "${LOG_TAG}" "$*" >&2; }
die() {
  log_err "$@"
  exit 2
}

usage() {
  cat <<EOF
Usage: ${APP_NAME} [options]

Install the Homebrew packages needed to develop the ADOS-on-Ollama toolkit:
  bash (>=5), shellcheck, shfmt, bats-core

Options:
  -h, --help     Show this help
  -n, --dry-run  Show what would be installed without doing it
EOF
}

main() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      -n | --dry-run) DRY_RUN=true ;;
      *) die "Unknown option: $1" ;;
    esac
    shift
  done

  command -v brew >/dev/null 2>&1 \
    || die "Homebrew not found. Install it from https://brew.sh first."

  log_info "Ensuring dev tools: ${BREW_PKGS[*]}"
  local pkg
  for pkg in "${BREW_PKGS[@]}"; do
    if brew list --formula "${pkg}" >/dev/null 2>&1; then
      log_info "skip    ${pkg} (already installed)"
    elif [[ "${DRY_RUN}" == "true" ]]; then
      log_info "[DRY-RUN] Would: brew install ${pkg}"
    else
      log_info "install ${pkg}"
      brew install "${pkg}"
    fi
  done

  log_info "Done. Put \"\$(brew --prefix)/bin\" ahead of /usr/bin on PATH so 'bash' is >=5."
  log_info "This shell's bash: ${BASH_VERSION}"
}

main "$@"
