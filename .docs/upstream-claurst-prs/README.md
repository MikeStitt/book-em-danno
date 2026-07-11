# Upstream claurst bug reports & PR drafts

These are **drafts to hand to upstream `Kuberwastaken/claurst`** — one file per fix,
each written so it can be split into a GitHub **issue** (the "Bug report" section) and a
**pull request** (the "PR" section + the diff). They are produced from the danno fork
(`MikeStitt/claurst`) where each shipped fix lives on its own branch off `upstream/main`
(`59c397f`). Nothing here opens a PR; that's a deliberate manual step.

## ⚠️ These are NOT six independent fixes — dependency graph

The original framing ("independent, send as four small PRs") was **wrong**, discovered
the hard way on 2026-07-10: Bugs 4 + 5 together **armed upstream's latent auto-compact
livelock** (Bug 8) — usage started flowing (5) against a small fallback window (4), the
99% trigger became permanently true, and claurst began dispatching unprompted compact
turns that ESC couldn't stop (Bug 9). Full chain: memory `claurst-auto-compact-armed` +
`.docs/claurst-integration.md`.

The honest structure — **Bug 8 is the required base; 4 and 5 stack on it**:

```
upstream/main (59c397f)
│
├── Bug 8  fix/auto-compact-safety            ★ BASE — must land first
│   │        (provenance-gate + re-arm latch for the auto-compact trigger)
│   ├── Bug 4  fix/ollama-context-window-fallback   STACKED on Bug 8
│   │            (edits the same refresh_context_window_size fallback arm —
│   │             textual conflict AND semantic dependency)
│   └── Bug 5  fix/ollama-nvidia-stream-usage       STACKED on Bug 8
│                (different files, semantic dependency: enabling usage for
│                 local providers is what arms the unlatched trigger)
│
├── Bug 9  fix/esc-cancels-query              independent (same user-facing
│                                             failure family as Bug 8)
├── Bug 6  fix/context-used-set-not-accumulate  independent — strictly REDUCES
│                                             spurious auto-compact firing
├── Bug 1  fix/configurable-provider-stall-timeout  independent
├── Bug 7  fix/warn-on-zero-tools             independent
└── Bug 3  tui-scroll-repaint-ghosting        issue-first (docs only)
```

**Merge/submission order:** Bug 8 first (or 8+4, 8+5 as combined PRs — acceptable per
the same logic); Bugs 4 and 5 declare "depends on Bug 8" in their PR text and branch
from `fix/auto-compact-safety`, not from `upstream/main`. Never present 4 or 5 as
standalone again. Bugs 1, 3, 6, 7, 9 remain genuinely independent.

| # | Draft | Fork branch | Type | Depends on |
|---|-------|-------------|------|------------|
| Bug 8 | [bug8-auto-compact-unsafe-trigger.md](bug8-auto-compact-unsafe-trigger.md) | _not yet written_ | issue-first, **base fix** | — |
| Bug 4 | [bug4-ollama-context-window-fallback.md](bug4-ollama-context-window-fallback.md) | `fix/ollama-context-window-fallback` | code fix | **Bug 8** |
| Bug 5 | [bug5-ollama-nvidia-stream-usage.md](bug5-ollama-nvidia-stream-usage.md) | `fix/ollama-nvidia-stream-usage` | code fix | **Bug 8** |
| Bug 9 | [bug9-esc-does-not-cancel-query.md](bug9-esc-does-not-cancel-query.md) | _not yet written_ | issue-first | — |
| Bug 6 | [bug6-context-used-set-not-accumulate.md](bug6-context-used-set-not-accumulate.md) | `fix/context-used-set-not-accumulate` | code fix | — |
| Bug 1 | [bug1-configurable-provider-stall-timeout.md](bug1-configurable-provider-stall-timeout.md) | `fix/configurable-provider-stall-timeout` | code fix | — |
| Bug 7 | [bug7-warn-on-zero-tools.md](bug7-warn-on-zero-tools.md) | `fix/warn-on-zero-tools` | code fix | — |
| Bug 3 | [bug3-tui-scroll-repaint-ghosting.md](bug3-tui-scroll-repaint-ghosting.md) | `fix/tui-scroll-repaint-ghosting` | issue-first (docs only) | — |

Why the "independence" notes on 6/1/7/9 hold:
- **Bug 6** changes the numerator from accumulate to set-to-latest — it can only *lower*
  the meter, so alone it strictly delays any Bug-8 firing (and is a real fix for cloud
  providers where usage already flows upstream).
- **Bug 9** is correct regardless of the meter; it just also happens to be the only way
  to *stop* a Bug-8 loop by hand.
- **Bugs 1, 3, 7** don't touch the meter or the event loop's dispatch conditions.

## danno-side counterpart (not an upstream PR)

danno generating + injecting `CLAURST_MODELS_PATH` (real `num_ctx`, with
`"tool_call": true` per Bug 7's footgun) makes the meter *honest* — but it is
**defense-in-depth, not a substitute for Bug 8**: a genuinely full window still ≥ 99% →
still livelocks without the latch. Both are needed. The danno fork release that follows
this restructure (`v0.1.6-danno2`) must be cut from a `danno-integration` re-merge that
includes Bug 8 (and ideally Bug 9) — until then, interactive claurst-under-danno sessions
will keep exhibiting the unprompted-compact behavior.

## ⚠️ Before opening any PR
The `fix/ollama-nvidia-stream-usage` branch on the fork **accidentally committed a built
binary** (`claurst-linux-arm64`) and a release tarball. **Exclude those from the upstream
PR** — cherry-pick only the `openai_compat_providers.rs` change (commit `f384b57`,
source-only). The diffs reproduced in these drafts already filter the binary out.
When re-parenting Bugs 4/5 onto `fix/auto-compact-safety`, that is the natural moment to
drop the binary from history.

## Verification status (danno fork, NVIDIA NIM + Ollama, 2026-06-26 → 2026-07-10)
- Bug 1 fix confirmed: 600s default lets large-context prefills (98–157s measured) complete.
- Bugs 5/6 confirmed against Ollama locally (meter "used" advances + no longer over-counts).
- Bug 7 confirmed: a catalog entry missing `tool_call` now prints the eprintln warning in
  headless `-p` runs instead of silently shipping 0 tools.
- Bug 8 confirmed live 2026-07-10 (interactive danno session, `v0.1.6-danno1`): unprompted
  "[Auto-compact triggered …]" turns after every completed turn once context passed ~8 K
  tokens; ESC unable to stop them (Bug 9 by inspection: no `KeyCode::Esc` in `main.rs`,
  token cancel gated on `is_streaming`).
