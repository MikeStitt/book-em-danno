#!/usr/bin/env bash
# install-ados.sh — install Agentic Delivery OS (ADOS) into a target project.
#
# ADOS's own installer puts agent/command definitions only in the global
# ~/.config/opencode, which a Docker sandbox cannot see. So in addition to
# running ADOS's --local install (docs, templates, .ai stubs), this copies the
# agent/command .md defs PROJECT-LOCAL into <target>/.opencode and records the
# ADOS provenance (source + commit) for traceable updates.
#
# Usage: install-ados.sh --target <dir> [options]
#   --target <dir>     Project to install into (default: .)
#   --ados-repo <dir>  ADOS checkout to copy from (default: auto-detected)
#   --global           Also run ADOS's global install (~/.config/opencode)
#   -f, --force        Pass --force to ADOS (overwrite project-specific files)
#   -n, --dry-run      Show what would be done without doing it
#   -v, --verbose      Debug output
#   -h, --help / -V, --version
#
# Env: ADOS_REPO (ADOS checkout), DRY_RUN, VERBOSE.
# Exit: 0 ok; 2 usage; 3 config; 4 runtime.
set -Eeuo pipefail
set -o errtrace
shopt -s inherit_errexit 2>/dev/null || true
IFS=$'\n\t'

readonly APP_NAME="install-ados"
readonly APP_VERSION="0.1.0"
LOG_TAG="(${APP_NAME})"
readonly EXIT_CONFIG=3

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=scripts/lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"

require_bash 4

TARGET_DIR="."
ADOS_REPO="${ADOS_REPO:-}"
DO_GLOBAL=false
FORCE=false

die_config() {
  log_err "$@"
  exit "${EXIT_CONFIG}"
}

# Resolve an ADOS checkout that contains .opencode/agent.
resolve_ados_repo() {
  if [[ -n "${ADOS_REPO}" ]]; then
    [[ -d "${ADOS_REPO}/.opencode/agent" ]] \
      || die_config "ADOS_REPO has no .opencode/agent: ${ADOS_REPO}"
    (cd "${ADOS_REPO}" && pwd -P)
    return 0
  fi
  local candidates=("${HOME}/.ados/repo" "${SCRIPT_DIR}/../../agentic-delivery-os")
  local c
  for c in "${candidates[@]}"; do
    if [[ -d "${c}/.opencode/agent" ]]; then
      (cd "${c}" && pwd -P)
      return 0
    fi
  done
  die_config "Cannot find an ADOS checkout. Pass --ados-repo <dir> or install ADOS globally."
}

# Copy *.md from src to dest, only when missing or changed.
copy_md_dir() {
  local src="$1" dest="$2" label="$3"
  [[ -d "${src}" ]] || {
    log_warn "missing ${label} source: ${src}"
    return 0
  }
  run_cmd mkdir -p "${dest}"
  local f name
  for f in "${src}"/*.md; do
    [[ -f "${f}" ]] || continue
    name="$(basename "${f}")"
    if [[ -f "${dest}/${name}" ]] && cmp -s "${f}" "${dest}/${name}"; then
      log_debug "skip ${label}/${name} (unchanged)"
    else
      run_cmd cp "${f}" "${dest}/${name}"
      log_info "copy ${label}/${name}"
    fi
  done
}

# Run ADOS's own --local installer (docs, templates, .ai stubs) in the target.
install_local_via_ados() {
  local ados="$1" target="$2"
  local installer="${ados}/scripts/install.sh"
  [[ -f "${installer}" ]] || die_config "ADOS installer not found: ${installer}"
  local -a ados_args=(--local --no-fetch)
  [[ "${FORCE}" == "true" ]] && ados_args+=(--force)
  if [[ "${DRY_RUN}" == "true" ]]; then
    log_info "[DRY-RUN] Would run (cwd=${target}): ADOS_SOURCE_DIR=${ados} bash ${installer} ${ados_args[*]}"
    return 0
  fi
  log_info "Running ADOS local install into ${target}"
  (cd "${target}" && ADOS_SOURCE_DIR="${ados}" bash "${installer}" "${ados_args[@]}")
}

write_provenance() {
  local ados="$1" target="$2"
  local sha date out
  sha="$(git -C "${ados}" rev-parse --short HEAD 2>/dev/null || printf 'unknown')"
  date="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  out="${target}/.opencode/ados-provenance.txt"
  if [[ "${DRY_RUN}" == "true" ]]; then
    log_info "[DRY-RUN] Would write ${out} (ADOS ${sha})"
    return 0
  fi
  mkdir -p "${target}/.opencode"
  printf 'ADOS source:  %s\nADOS commit:  %s\nInstalled:    %s\nInstalled by: claude-setup-agentic-delivery-os/install-ados.sh\n' \
    "${ados}" "${sha}" "${date}" >"${out}"
  log_info "wrote ${out} (ADOS ${sha})"
}

usage() {
  cat <<EOF
Usage: ${APP_NAME} --target <dir> [options]

Install ADOS into a target project: ADOS's docs/templates/.ai (via its own
--local installer) PLUS the agent/command definitions copied project-local so a
Docker sandbox can see them, with an ADOS provenance record. Idempotent.

Options:
  --target <dir>     Project to install into (default: .)
  --ados-repo <dir>  ADOS checkout to copy from (default: auto-detected)
  --global           Also run ADOS's global install (~/.config/opencode)
  -f, --force        Pass --force to ADOS (overwrite project-specific files)
  -n, --dry-run      Show what would be done without doing it
  -v, --verbose      Debug output
  -h, --help         Show this help
  -V, --version      Show version
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
      --target)
        shift
        TARGET_DIR="${1:?--target requires a dir}"
        ;;
      --ados-repo)
        shift
        ADOS_REPO="${1:?--ados-repo requires a dir}"
        ;;
      --global) DO_GLOBAL=true ;;
      -f | --force) FORCE=true ;;
      -n | --dry-run) DRY_RUN=true ;;
      -v | --verbose) VERBOSE=true ;;
      *) die "Unknown option: $1" ;;
    esac
    shift
  done

  require_cmd git

  local ados target
  ados="$(resolve_ados_repo)"
  target="$(cd "${TARGET_DIR}" 2>/dev/null && pwd -P)" \
    || die_config "Target directory not found: ${TARGET_DIR}"
  [[ -d "${target}/.git" ]] \
    || log_warn "Target is not a git repo root: ${target} (ADOS --local may refuse)"

  log_info "ADOS source: ${ados}"
  log_info "Target:      ${target}"

  if [[ "${DO_GLOBAL}" == "true" ]]; then
    log_info "Running ADOS global install"
    run_cmd bash "${ados}/scripts/install.sh" --global
  fi

  install_local_via_ados "${ados}" "${target}"
  copy_md_dir "${ados}/.opencode/agent" "${target}/.opencode/agent" "agent"
  copy_md_dir "${ados}/.opencode/command" "${target}/.opencode/command" "command"
  write_provenance "${ados}" "${target}"

  log_info "Done. ADOS installed into ${target} (.opencode/{agent,command} + docs)."
}

if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
