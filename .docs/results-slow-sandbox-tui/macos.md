# macOS · zsh — slow-sandbox-tui results
date: 2026-07-22
host OS: macOS 26.2 (arm64) · shell: zsh · Python 3.14.2
sbx v0.34.0 · pexpect 4.9.0 · pyte 0.8.2 · driver: PexpectDriver
fidelity: host-pty (real `sbx exec -it … <argv>`)
harness builds: opencode (sandbox image `opencode-docker`) · codex 0.144.5 · claurst 0.1.6-danno1

All three A/H/C green via `uv run pytest tests/slow/tui/test_tui_launch.py -m sandbox`
(run per-harness; the fixed proxy/stub ports mean they run serially, not `-n`):

| harness  | A | H | C | classification | leg | notes / root-cause |
|----------|---|---|---|----------------|-----|--------------------|
| opencode | ✅ | ✅ | ✅ | works | — | 134s. Compaction request on `/chat/completions` (anchored-summary marker). Late auto-update modal ESC'd by the settle window; one-shot inflation → exactly one compaction. |
| codex    | ✅ | ✅ | ✅ | works | — | 137s. Top-level `model_auto_compact_token_limit=200` grafted into `config.toml`; `CONTEXT CHECKPOINT COMPACTION` request on `/responses`. First-run trust dialog answered `1`+Enter. |
| claurst  | ✅ | ✅ | 0 | works (compacts=False change-detector) | — | 209s. Usage flows (`stream_options.include_usage`) but claurst v0.1.6-danno1 does NOT auto-compact even at 2M tokens → `summarization_requests == 0` asserted. First-run "Keyboard Shortcuts" overlay ESC'd; composer glyph `❯`. |

Full gate green alongside: `ninja check` → 683 passed, 34 deselected (the 3 sandbox tests
correctly out of the fast gate).

Driver notes for the Windows lane:
- `PexpectDriver` drove the real launch frame verbatim (`_CaptureLaunchRunner` intercepts only
  the terminal handoff); `TERM` forced off `dumb` → `xterm-256color`; geometry 160×48.
- One benign `DeprecationWarning: forkpty() may lead to deadlocks` from pexpect on Python 3.14 —
  no functional impact (the fork's child immediately execs sbx).
- The `TuiDriver` protocol is FROZEN as of this run (P1 exit gate) — `WinPtyDriver` builds
  against it unchanged.
