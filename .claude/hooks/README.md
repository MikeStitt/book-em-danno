# Claude Code hooks

Shell hooks wired up via [`../settings.json`](../settings.json). Each hook is a
tracked shell script so it's reviewable and shared with the team.

## `precompact-checkpoint.sh`

Triggered on `PreCompact` (just before Claude Code compacts the conversation,
either via `/compact` or automatically when context fills). Writes a snapshot
of current work state — git branch, recent commits, working tree, plan
markers — to the per-user Claude auto-memory directory so the post-compact
Claude can recover quickly.

Output goes to:

```text
$HOME/.claude/projects/<sanitized-cwd>/memory/project_last_compact_state.md
```

The script also self-indexes by appending a pointer to that directory's
`MEMORY.md` (only once, idempotent).

Disabling for a single developer: override in
`.claude/settings.local.json` (precedence: user < project < local) by setting
`disableAllHooks: true` or by writing an empty `hooks.PreCompact: []`.
