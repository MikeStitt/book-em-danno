# Project Conversion Plan: Bash to Python 3.13

## Status: Phase 0 - Requirements & UX Design
- [x] Requirements Gathering (Analysed scripts/tools)
- [x] UX Documentation (Created docs/ux-requirements.md)
- [ ] Review & Ratification
- [ ] **Constitution drift follow-up (tracked):** the ratified constitution
  (`.specify/memory/constitution.md`, v1.1.1) still describes a **Bash-first**
  repo whose **reason to exist is installing ADOS into target projects**, with
  `make check` running shellcheck/shfmt/bats. This conversion (Python + ADOS as
  one configurable tool in the catalog, not a hard dependency) diverges from
  that. Recommend amending the constitution and `parts/` (bash.md, testing.md,
  ados-ollama.md) via the constitution's own amendment workflow **before** the
  legacy `.sh` files are removed in Phase 4. Not done in the current scope.

## High Level Phases

### Phase 0: Requirements & UX Design
- Analyze current Bash tool behavior and identify all necessary features.
- Create a comprehensive UX document (Commands, Responses, OS-specific guidance) —
  see [docs/ux-requirements.md](ux-requirements.md). All work must adhere to it.
- Document the end-to-end user story for "Zero to AI Coding":
  - Declaration (`danno.toml` is the source of truth the user edits).
  - Provisioning (`danno install`, `danno doctor`) — generate the OpenCode config
    from `danno.toml`; host project stays clean.
  - Equipping (`danno tools install`) — install the catalog (ADOS, plannotator,
    opencode-planner) per-tool into the sandbox or project-local.
  - Isolation (`danno sandbox start` → Docker Sandbox).
  - Operation (OpenCode TUI interaction).
  - Debugging (`danno sandbox shell`).
  - Tuning (edit `danno.toml` → `danno config generate`, diff-for-approval).
- Agreement on requirements and UX.

### Phase 1: Foundation
- Initialize `uv` project (Python 3.13).
- Configure `pyproject.toml` for package distribution (with `typer`, `rich`,
  `pydantic`, `pytest`). Entry points `danno` and `book-em-danno`.
- Set up quality gates (`ruff`, `mypy`, `pytest`) **alongside** the existing
  shell hooks — do not replace or break the Bash gates.
- Define the `danno.toml` schema (pydantic) and loader (tomllib).

### Phase 2: Core Implementation
- Cross-platform core library (I/O, logging, command execution) using `pathlib`;
  port the `log_*` / `run_cmd` / dry-run idioms from `scripts/lib/common.sh`.
- Port Bash tools to Python entry points under the `danno` CLI (`install`,
  `doctor`, `ollama`, `sandbox`, `config`, `tools`).
- **`danno.toml` → `.opencode/opencode.jsonc` generator**: port the agent-tier
  mapping from `tools/gen-opencode-config`; first-run writes, later runs propose
  a diff (Tier-1 of the automation policy).
- **Backend abstraction**: `ollama` + `cloud` implemented; `llamacpp` stubbed
  (schema slot present, generator raises a clear "not yet implemented").
- **Tool catalog installer**: resolve per-tool `install_to` (sandbox vs project)
  and emit install commands; run only under `--apply`.
- Implement the **automation policy**: Tier-1 config files auto/diff; Tier-2 host
  & Docker actions copy-paste by default, executed only with `--apply`.
- Implement OS-aware guidance for Docker and Ollama setup.
- Standardize on **Docker Sandbox** as the primary isolation method across all 3
  supported OSs.

### Phase 3: Testing & Validation
- Migrate tests to `pytest`.
- Create cross-platform test matrices (Win/Mac/Lin).
- Implement "Mock mode" for testing without Docker/Ollama dependencies.
- Verify parity with original functionality.

### Phase 4: Finalization
- Update documentation and `Makefile`.
  - Rewrite `README.md` to reflect the name change (ADOS Toolkit → book-em-danno /
    OpenCode Orchestrator).
  - Refocus value proposition: emphasize the tool as the bridge between local
    Ollama (GPU) and an isolated Docker sandbox for OpenCode, driven by `danno.toml`.
  - Reframe ADOS: it becomes **one configurable tool in the catalog** (per-tool
    `install_to`), not a hard dependency. Keep it as an option; drop the
    requirement that it always be copied into the project.
  - Update Quickstart guides: replace legacy paths like `tools/ados-ollama install`
    with the new `danno install` flow.
  - Update the "What it puts on your Mac" table for the new flow.
- Resolve the **constitution drift follow-up** (Phase 0) before removing `.sh` files.
- Validate package installation via `uv`.
- Remove legacy `.sh` files.
