# Constitution — Testing part

Authoritative testing taxonomy for the `danno` Python CLI. Read this part when
writing or organizing tests, alongside [`python.md`](python.md) and the
[constitution](../constitution.md). Tests are a first-class artifact; the fast
suite runs in `ninja check` (the Quality Gate).

**Test-Driven Development is strongly encouraged.** Write tests before
implementation — especially for the install flow, where a regression silently
corrupts a target repo.

## Framework & homes

- **`pytest`**, run via `uv run pytest`. Tests live under `tests/`; shared test
  helpers (e.g. a recording `Runner`) live in `tests/conftest.py`.
- **Two suites, split by a marker** (registered in `pyproject.toml`):
  - **fast `tests/`** — the default; no daemon, no network. `ninja check` runs
    `pytest -q -m "not slow"`.
  - **slow `tests/slow/`** — `@pytest.mark.slow`; talk to **real** Ollama/Docker.
    They `skipif` the daemon is down or Ollama is unreachable, so they collect and
    skip cleanly on a cold host. Run them with `uv run pytest -m slow`.

## What to test (and how)

- **Command construction (fast, mock-free).** Drive `Runner.advise` through a
  recording `Runner` and assert the **exact** `docker sandbox …` / `ollama pull`
  command strings and their **ordering** — no Docker/Ollama needed. This is the
  bulk of the fast suite.
- **Config gen / loader.** Render `.opencode/opencode.jsonc` from a real-tag
  `danno.toml`; assert the agent→model map, and that a bad config exits non-zero.
- **Idempotency & preservation** (constitution Working Rule 6): first-run write,
  diff-then-stop on change, no-op when identical; a hand-edited config is not
  clobbered without `--apply`.
- **Doctor predicates** via monkeypatched probes — assert PASS/FAIL/WARN counts
  and that loopback-bind is a WARN, not a failure.
- **Live behavior (slow):** Ollama `/api/generate` responds; the tool-call probe
  asserts the local model emits `tool_calls`; a guarded end-to-end creates a
  throwaway sandbox and asserts `docker sandbox exec <n> opencode --version`
  **in-container**, then tears it down.

## The in-container invariant

Tests **NEVER** invoke host `opencode`. OpenCode only ever runs inside the Docker
sandbox; any opencode assertion goes through `docker sandbox exec`. The
interactive TUI is not asserted.

## See also

- [`../constitution.md`](../constitution.md) — Working Rules + Quality Gates.
- [`python.md`](python.md) — the Runner, the two-tier policy, and the layout.
