# Project Conversion Plan: Bash to Python 3.13

## Status: Phase 0 - Requirements & UX Design
- [x] Requirements Gathering (Analysed scripts/tools)
- [x] UX Documentation (Created docs/ux-requirements.md)
- [ ] Review & Ratification

## High Level Phases

### Phase 0: Requirements & UX Design
- Analyze current Bash tool behavior and identify all necessary features.
- Create a comprehensive UX document (Commands, Responses, OS-specific guidance).
- Document the end-to-end user story for "Zero to AI Coding":
  - Provisioning (`danno install`, `danno doctor`)
  - Isolation (`danno sandbox start` -> Docker Sandbox)
  - Operation (OpenCode TUI interaction)
  - Debugging (`danno sandbox shell`)
  - Tuning (`danno config generate`)
- Agreement on requirements and UX.

### Phase 1: Foundation
- Initialize `uv` project (Python 3.13).
- Configure `pyproject.toml` for package distribution.
- Set up quality gates (`ruff`, `mypy`, `prek`).

### Phase 2: Core Implementation
- Cross-platform core library (I/O, logging, command execution).
- Port Bash tools to Python entry points under the `danno` CLI.
- Implement OS-aware guidance for Docker and Ollama setup.
- Standardize on **Docker Sandbox** as the primary isolation method across all 3 supported OSs.

### Phase 3: Testing & Validation
- Migrate tests to `pytest`.
- Create cross-platform test matrices (Win/Mac/Lin).
- Verify parity with original functionality without ADOS dependencies.

### Phase de 4: Finalization
- Update documentation and `Makefile`.
- Validate package installation via `uv`.
- Remove legacy `.sh` files.

