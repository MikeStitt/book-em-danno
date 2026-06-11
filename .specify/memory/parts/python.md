# Constitution — Python part

Authoritative standards for the `danno` Python CLI. Read this part when writing
or changing code under `src/book_em_danno/` or `tests/`. Read together with the
[constitution](../constitution.md) and [`testing.md`](testing.md).

## Toolchain

- **Package/runtime manager: `uv`.** Run everything through `uv run …`; deps are
  locked in `uv.lock`. Python **>= 3.13**.
- **CLI framework: `typer`**; **output: `rich`** (one shared `Console` in
  `core/exec.py`); **config models: `pydantic` v2**.
- **Gate: `ninja check`** = `ruff check .` + `ruff format --check .` + `mypy` +
  `pytest -q -m "not slow"`. Definition lives in `build.ninja`. `ruff` line length
  100; `mypy` runs with `disallow_untyped_defs` — every function is typed.

## Architecture (matches the as-built layout)

- `config/` — the declarative source of truth: `schema.py` (pydantic models),
  `loader.py` (tomllib + loud validation at the boundary), `generate.py` (renders
  `.opencode/opencode.jsonc`, first-run write / diff-on-change).
- `core/exec.py` — logging helpers + the **`Runner`**.
- `commands/` — `doctor`, `ollama`, `sandbox`, `tools`, `install`. These are
  internals orchestrated by `install`; only `install` / `doctor` / `sandbox` are
  exposed as CLI commands (`cli.py`).

## The two-tier automation policy (the defining rule)

Every host/Docker/Ollama side effect goes through `Runner.advise(cmd, why)`:

- **default** — print the literal copy-paste command; run nothing (the user runs
  it).
- **`--dry-run` / `-n`** — print only, never execute (wins over `--apply`).
- **`--apply`** — print **and** execute via `subprocess.run`.

`advise()` returns the command list so tests assert exact construction without a
daemon. **Tier-1** files we own (`.opencode/opencode.jsonc`) are written on first
run, diffed-then-stopped when they'd change (needs `--apply`), no-op when
identical. **Tier-2** external effects (`ollama pull`, `docker sandbox …`, tool
installers) are advised by default.

## Discipline (extends the core Working Rules)

- **Small, side-effect-free core; I/O at the edges.** Pure logic (config
  rendering, command construction) stays free of subprocess/network so it is
  unit-testable; `subprocess`/`urllib` live in thin wrappers (`Runner`,
  `commands/ollama.py` probes).
- **Validate at the boundary, fail loud** (Working Rule 8). The pydantic schema
  + loader reject unknown keys and dangling references with a non-zero exit; never
  silently produce a wrong `opencode.jsonc`.
- **Never fabricate an external mechanism.** If a tool's install path is unknown
  (e.g. plannotator), emit an explicit advisory/TODO — do not invent a command.
- **OpenCode runs ONLY in the Docker sandbox**, never on the host — a hard
  invariant for the CLI *and the tests* (see [`ados-ollama.md`](ados-ollama.md)
  and [`testing.md`](testing.md)).

## See also

- [`../constitution.md`](../constitution.md) — Working Rules + Quality Gates.
- [`testing.md`](testing.md) — pytest fast/slow split and the in-container rule.
- [`ados-ollama.md`](ados-ollama.md) — what danno provisions and the network model.
