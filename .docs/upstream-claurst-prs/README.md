# Upstream claurst bug reports & PR drafts

These are **drafts to hand to upstream `Kuberwastaken/claurst`** — one file per fix,
each written so it can be split into a GitHub **issue** (the "Bug report" section) and a
**pull request** (the "PR" section + the diff). They are produced from the danno fork
(`MikeStitt/claurst`) where each fix already lives on its own branch off `upstream/main`
(`59c397f`). Nothing here opens a PR; that's a deliberate manual step.

| # | Draft | Fork branch | Type |
|---|-------|-------------|------|
| Bug 1 | [bug1-configurable-provider-stall-timeout.md](bug1-configurable-provider-stall-timeout.md) | `fix/configurable-provider-stall-timeout` | code fix |
| Bug 3 | [bug3-tui-scroll-repaint-ghosting.md](bug3-tui-scroll-repaint-ghosting.md) | `fix/tui-scroll-repaint-ghosting` | issue-first (docs only) |
| Bug 4 | [bug4-ollama-context-window-fallback.md](bug4-ollama-context-window-fallback.md) | `fix/ollama-context-window-fallback` | code fix |
| Bug 5 | [bug5-ollama-nvidia-stream-usage.md](bug5-ollama-nvidia-stream-usage.md) | `fix/ollama-nvidia-stream-usage` | code fix |
| Bug 6 | [bug6-context-used-set-not-accumulate.md](bug6-context-used-set-not-accumulate.md) | `fix/context-used-set-not-accumulate` | code fix |
| Bug 7 | [bug7-warn-on-zero-tools.md](bug7-warn-on-zero-tools.md) | `fix/warn-on-zero-tools` | code fix |

Bugs 4/5/6/7 are the "context meter is wrong / silent tool failure" cluster; they are
independent and can be sent as four small PRs (or one combined "local-provider UX" PR —
4+5+6 all touch the same surface). Bug 1 is standalone. Bug 3 is issue-first: a documented
known-issue with a repro and a fix direction, no confident fix yet.

## ⚠️ Before opening any PR
The `fix/ollama-nvidia-stream-usage` branch on the fork **accidentally committed a built
binary** (`claurst-linux-arm64`) and a release tarball. **Exclude those from the upstream
PR** — cherry-pick only the `openai_compat_providers.rs` change (commit `f384b57`,
source-only). The diffs reproduced in these drafts already filter the binary out.

## Verification status (danno fork, NVIDIA NIM, 2026-06-26/27)
- Bug 1 fix confirmed: 600s default lets large-context prefills (98–157s measured) complete.
- Bugs 5/6 confirmed against Ollama locally (meter "used" advances + no longer over-counts).
- Bug 7 confirmed: a catalog entry missing `tool_call` now prints the eprintln warning in
  headless `-p` runs instead of silently shipping 0 tools.
