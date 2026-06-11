# Changelog

All notable changes to `danno` (book-em-danno). Generated from conventional
commits by [git-cliff](https://git-cliff.org).

## [0.1.0] - 2026-06-11

### Bug Fixes

- Track .specify parts and merge JetBrains gitignore template
- Config defaults (top-level model + literal baseURL) and CI lint
- Make local Gemma work in the Docker sandbox (verified end-to-end)
- Stop .gitignore from ignoring scripts/lib/common.sh
- *(sandbox)* Correct docker sandbox rm flags and make provisioning idempotent
- *(test)* Stop-then-rm in slow e2e teardown (no invalid -f flag)

### Build System

- *(release)* Add git-cliff changelog config and release flow

### CI

- Fix shellcheck source-following and Linux tool install
- Run scripts under brew bash 5 on macOS
- Ados-sandbox --dry-run must not require docker
- Publish GitHub Release from git-cliff on vX.Y.Z tag push

### Documentation

- Add living plan tracker and UX doc; exempt .docs from doc checks
- M8 — README + sync constitution to as-built (v1.1.0)
- README quickstart for elephant with gemma4:26b-mlx
- Two named quickstart paths + record gemma4 tool-calling verified
- Add a third quickstart path — fully local gemma4:26b-mlx (no cloud)
- DRY the network model into the README (constitution v1.1.1)
- Add measured context-window memory table + how to change it
- Generated CLI reference + state macOS-only platform
- Revise UX + conversion plan for danno.toml-driven config

### Features

- Initial consitution
- Add M0 repo foundation and make-check quality gate
- Add M1 preflight doctor and shared common.sh
- Add M2 ollama provisioning (setup-ollama.sh)
- Add M3 ADOS install integration (install-ados.sh)
- Add M4 hybrid model-config generator (gen-opencode-config)
- Add M5 docker sandbox launcher (ados-sandbox)
- Add M6 orchestrator (ados-ollama)
- Add M7 experimental macOS Seatbelt sandbox (ados-sandbox-macos)
- Add --cloud-only / --local-only to gen-opencode-config
- Open internet egress in the sandbox + document the network model
- Scaffold Python danno CLI with danno.toml -> opencode.jsonc generator
- Initial version
- *(cli)* Add --version flag
- *(sandbox)* Parametrize agent and inject Claude Code auth

### Testing

- *(slow)* Drop misplaced skipif on _teardown_sandbox helper

