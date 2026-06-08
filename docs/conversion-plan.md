# Project Conversion Plan: Bash to Python 3.13

## Status: Phase 0 - Requirements & UX Design
- [x] Requirements Gathering (Analysed scripts/tools)
- [x] UX Documentation (Created docs/ux-requirements.md)
- [ ] Review & Ratification

## High Level Phases

### Phase 0: Requirements & UX Design
- Analyze current Bash tool behavior and identify all necessary features.
- Create a comprehensive UX document (Commands, Responses, OS-specific guidance).
- Document the steps one would do (based upon @README.md and adjustments):
  - The steps include what the user's goal is, the tools and options they run,
    approximately what the tool returns (including copy and paste suggestions),
    a description of the system state after each step.
    - Install open-code pointing to local ollama or cloud AI in the project
    - build the docker sandbox
    - run opencode in the docker sandbox
    - connect to a bash shell in the docker sandbox
    - adjust the opencode configuration in an existisng docker sandbox
- Agreement on requirements and UX.

### Phase 1: Foundation
- Initialize `uv` project (Python 3.13).
- Configure `pyproject.toml` for package distribution.
- Set up quality gates (`ruff`, `mypy`, `prek`).

### Phase 2: Core Implementation
- Cross-platform core library (I/O, logging, command execution).
- Port Bash tools to Python entry points based on UX.
- Implement OS-aware command suggestions for Docker/Ollama.

### Phase 3: Testing & Validation
- Migrate tests to `pytest`.
- Create cross-platform test matrices.
- Verify parity with original functionality without ADOS dependencies.

### Phase 4: Finalization
- Update documentation and `Makefile`.
- Validate package installation via `uv`.
- Remove legacy `.sh` files.
