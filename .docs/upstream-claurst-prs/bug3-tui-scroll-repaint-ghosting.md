# Bug 3 — TUI leaves ghost/overlapping text when the terminal is scrolled (issue-first)

Branch: `fix/tui-scroll-repaint-ghosting` (commit `f67f33c`, off `upstream/main` 59c397f)
File added: `docs/known-issues/tui-scroll-repaint-ghosting.md`

> **Issue-first:** no confident fix yet. This draft is intended primarily as a **GitHub
> issue**. The fork branch only adds a known-issue doc (no code change), so the "PR" here is
> optional — it just lands the documented repro + investigation direction.

---

## Bug report (→ GitHub issue)

**Title:** TUI leaves ghost/overlapping text when the terminal window is scrolled

**Symptom**

Scrolling the terminal window during a session leaves **ghost/overlapping text**: fragments
of longer lines bleed into the right side and line-number/content interleave
(e.g. `2d file`, `3ers/mikeChains…`). Reproduced in Ghostty (host `TERM=xterm-256color`).

This is **not** a terminal/locale issue: a non-UTF-8 VM locale produces *different*,
box-drawing glitches (see "Related" below) — a separate defect.

**Suspected root cause**

Incomplete cell-clearing on **scroll repaint** in the ratatui TUI — vacated cells are not
blanked when the viewport scrolls, so stale glyphs from longer prior lines remain.

**Workarounds (confirmed)**
- `Ctrl-L` forces a full redraw and clears the ghosts.
- Resizing the window a hair (`SIGWINCH` → full repaint) also clears them.
- Avoid mouse-wheel scrolling the host window.

**Proposed investigation / fix direction**
1. Audit the draw path (`crates/tui/src/render.rs`) for partial-area redraws that don't
   `Clear`/blank the vacated region on scroll.
2. Repro harness: a long transcript + programmatic scroll; assert no residual cells.
3. Candidate fix: explicitly clear the scrolled-away region (or force a full-frame redraw on
   scroll) before repainting.

**Related (separate) — non-UTF-8 locale box-drawing**

When run in a non-UTF-8 locale (`LC_CTYPE=POSIX`), Unicode box-drawing degrades
independently of this bug; pass `LC_ALL=C.utf8`. Not the same defect.

---

## PR (optional — docs only)

**Title:** docs(known-issues): document TUI scroll-repaint ghosting (issue-first)

Adds `docs/known-issues/tui-scroll-repaint-ghosting.md` capturing the symptom, suspected
cause, confirmed workarounds, and a fix direction — so the issue is discoverable until the
repaint path is isolated and fixed. No behavior change.
