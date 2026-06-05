#!/usr/bin/env bash
# PreCompact hook: snapshot current work state to the project's Claude auto-memory
# directory so the post-compact Claude can recover quickly. Triggered automatically
# by Claude Code before any conversation compaction (manual or auto).
#
# Wired up in .claude/settings.json under hooks.PreCompact.

set -euo pipefail

# Drain stdin (hook input JSON); not needed by this script.
cat > /dev/null

REPO_ROOT="$(git rev-parse --show-toplevel 2> /dev/null || pwd)"
SANITIZED_PROJECT_PATH="${REPO_ROOT//\//-}"
MEMORY_DIR="$HOME/.claude/projects/${SANITIZED_PROJECT_PATH}/memory"
OUT="$MEMORY_DIR/project_last_compact_state.md"
INDEX="$MEMORY_DIR/MEMORY.md"
PLAN_FILE=".docs/mesh-atlas-bootstrap-plan.md"

mkdir -p "$MEMORY_DIR"

{
  echo "---"
  echo "name: last-compact-state"
  echo "description: Auto-generated snapshot of work state at the most recent compaction (PreCompact hook)"
  echo "metadata:"
  echo "  type: project"
  echo "---"
  echo
  echo "_Captured by the PreCompact hook at $(date '+%Y-%m-%d %H:%M:%S %z'). Overwrites itself each compaction._"
  echo
  echo "## Branch"
  echo
  echo '```'
  git -C "$REPO_ROOT" branch --show-current
  echo '```'
  echo
  echo "## Recent commits"
  echo
  echo '```'
  git -C "$REPO_ROOT" log --oneline -10
  echo '```'
  echo
  echo "## Working tree"
  echo
  echo '```'
  status_output="$(git -C "$REPO_ROOT" status --short | head -30)"
  if [ -z "$status_output" ]; then
    echo "(clean)"
  else
    printf '%s\n' "$status_output"
  fi
  echo '```'
  if [ -f "$REPO_ROOT/$PLAN_FILE" ]; then
    echo
    echo "## Plan markers (IN PROGRESS + COMPLETED)"
    echo
    echo '```'
    # shellcheck disable=SC2016
    # Single quotes are intentional: the regex contains literal backticks and
    # parentheses that must not be interpreted by the shell.
    grep -nE '^- `- (IN PROGRESS|COMPLETED)`' "$REPO_ROOT/$PLAN_FILE" | head -30 || true
    echo '```'
  fi
} > "$OUT"

# Self-index: make sure MEMORY.md links to this snapshot so the post-compact
# Claude actually sees it.
ENTRY_LINE='- [Last compact state](project_last_compact_state.md) — Auto-generated snapshot of where the previous Claude was; written by the PreCompact hook just before this conversation was compacted.'
if [ -f "$INDEX" ]; then
  if ! grep -qF '(project_last_compact_state.md)' "$INDEX"; then
    # Ensure the existing index ends with a newline before appending.
    if [ -s "$INDEX" ] && [ -n "$(tail -c1 "$INDEX")" ]; then
      printf '\n' >> "$INDEX"
    fi
    printf '%s\n' "$ENTRY_LINE" >> "$INDEX"
  fi
else
  printf '%s\n' "$ENTRY_LINE" > "$INDEX"
fi

echo "PreCompact: wrote $OUT" >&2
