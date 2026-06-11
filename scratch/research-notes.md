# Phase 2 research notes (scratch; throwaway)

Findings captured while building the Python `danno` runtime (2026-06-11).

## Host state (probed)
- `opencode` 1.15.7, `uv` 0.9.21, `ninja` present
  (`/Library/Frameworks/Python.framework/Versions/3.13/bin/ninja`).
- Docker CLI + `docker sandbox` subcommand present; **daemon DOWN**.
- Ollama reachable on **127.0.0.1:11434 (loopback)** — fine from host, NOT from a
  sandbox VM. Fix: `OLLAMA_HOST=0.0.0.0:11434 ollama serve`.
- Models pulled: `gemma4:26b`, `gemma4:26b-mlx`, `qwen3.6:27b-q4_K_M`, `gemma3:1b`
  (NOT tool-capable). `gemma3:27b` NOT pulled — demo config avoids it.

## Live-verified during the build
- The slow Ollama tests (`pytest -m slow`) **ran and passed** against host Ollama:
  `gemma4:26b` responds on `/api/generate` AND emits `tool_calls` on `/api/chat`.
  So `gemma4:26b` is confirmed tool-capable and usable for ADOS agents.
- The Docker end-to-end slow test **skipped** (daemon down) — expected.

## Behavioral notes
- The Python Runner inverts the Bash default: Bash `run_cmd` executes unless
  DRY_RUN; ours **advises** (prints copy-paste) unless `--apply`. `--dry-run`
  always wins over `--apply`.
- Default sandbox name is `danno-<basename(target)>` (was `ados-…` in Bash).
- First-run config under `--dry-run` prints the full proposed file but labels it
  "differs from the existing file" — cosmetic carry-over from Phase 1 generate().
- OpenCode is never run on the host: the slow e2e asserts `opencode --version`
  only via `docker sandbox exec`.
