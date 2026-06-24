# Changelog

All notable changes to `danno` (book-em-danno). Generated from conventional
commits by [git-cliff](https://git-cliff.org).

## [0.7.0] - 2026-06-23

### Bug Fixes

- *(validator)* Make the SWE-bench suite actually run end-to-end

### Documentation

- *(capture)* Frame --capture coverage by the three model roles
- *(validator)* Plan Claurst as an agent-under-test + SWE benchmark tiers

### Features

- Improve danno.toml.example
- *(validator)* Disable opencode title generation during sweeps
- *(capture)* Record opencode<->backend wire traffic via --capture
- Improve danno.toml.example
- Remove tool_call
- Remove tool_call
- *(validator)* Drive Claurst as an agent-under-test (M1)
- *(validator)* Select Claurst as a sweep agent-under-test (M2/M3)
- *(validator)* Add benchmark-task abstraction + suite config (M4)
- *(validator)* Aider Polyglot benchmark suite (M5)
- *(validator)* SWE-bench Verified suite via HuggingFace (M6)
- *(validator)* Danno bench — run suites across the model matrix (M7)

## [0.6.0] - 2026-06-22

### Documentation

- *(example)* Use unique backend names in danno.toml.example
- Research+proposal for agents & cloud-backend refactor
- Plannotator-in-docker-sandbox research, comparison, and tunnel plan
- Mark agents-and-cloud-backend-refactor as implemented
- *(sandbox-agents)* Guide to the prebuilt sandbox agents

### Features

- *(config)* Rich [agents] form + markdown-collision warnings
- *(config)* Merge managed regions in opencode.jsonc + route agent models to .md
- *(validator)* Add `danno benchmark` to compare whole configs for editing perf

### Refactor

- *(sandbox)* Make `shell` mirror `start`, share session core
- *(config)* Inline [agents] refs, retire the cloud backend

### Testing

- *(config)* Add maximal danno.toml fixture to restore lost coverage

## [0.5.1] - 2026-06-18

### Documentation

- *(validator)* Decompose the bundled M7 into M7/M8/M9+

## [0.5.0] - 2026-06-18

### Features

- *(validator)* Inject cloud credentials into the validate sweep
- *(validator)* Add the L2 dev-quality judge (completes M6)

## [0.4.0] - 2026-06-18

### Bug Fixes

- *(validator)* Pass claude auth env-file to the baseline exec
- *(validator)* Prune stale per-config pages on report re-run
- *(cli)* Drive the sweep with opencode's run-agent, not the Docker agent
- *(cli)* Escape [agents] in the validate summary so rich doesn't eat it

### Documentation

- *(validator)* Record M5 live Claude Code baseline verification
- *(validator)* Record pinned-model live verification
- *(validator)* Record M6 judge scope decision (deferred, L2-only)
- *(validator)* Design the `danno validate` CLI (UX proposal)
- *(pyproject)* Clarify the [validator] extra gates deps, not the command

### Features

- *(validator)* Add agent-agnostic Turn seam + Claude Code driver
- *(validator)* Add Claude Code baseline row + report flag
- *(validator)* Pin and track the Claude baseline model
- *(validator)* Emit annotated "menu" danno.toml from a sweep
- *(validator)* Add a progress-event seam to the tiered runner
- *(validator)* Serialize sweep results to a results.json run record
- *(cli)* Add the `danno validate` command

### Testing

- *(validator)* Patch _teardown so orchestration tests don't shell docker

## [0.3.0] - 2026-06-17

### Bug Fixes

- *(install)* Fail loud when a tool installer fails
- *(sandbox)* Keep opencode data dir VM-local to avoid the WAL crash
- *(sandbox)* Pre-accept claude workspace trust; don't error on TUI exit
- *(sandbox)* Start an existing stopped VM before configuring its proxy

### Documentation

- Document the `sandbox start -- <agent args>` passthrough
- *(validator)* Record M3 live tiered-sweep verification

### Features

- *(config)* Emit every defined model, pull every defined ollama tag
- *(ollama)* Pull only models absent from `ollama list`
- *(sandbox)* Forward args after `--` to the agent on `sandbox start`
- *(config)* Add `openai` backend kind for authenticated OpenAI-compatible APIs
- *(sandbox)* Fail loud when opencode.jsonc references an unsupplied env var
- *(validator)* Add danno_validator M0 headless primitives
- *(validator)* Add M1 Level-0 liveness battery, stall oracle, MyST report
- *(validator)* Add M2 config-matrix sweep + results-matrix index
- *(validator)* Add M3 Level-1 tool/bash oracle + tiered L0→L1 sweep
- *(validator)* Add M4 Level-2 dev oracle (hidden test suite) + tiered L0→L1→L2 sweep

### Testing

- *(cli)* Assert --apply via parsed Click command, not rendered help
- *(cli)* Make default-install test hermetic (no ADOS dependency)

## [0.2.5] - 2026-06-14

### CI

- Keep uv.lock in sync across releases and enforce it

### Documentation

- *(constitution)* Add stacking rule to Branch & Push Policy (v2.1.0)

### Features

- *(cli)* Collapse automation modes to advise/--apply

## [0.2.4] - 2026-06-13

### Documentation

- Clarify release steps are GitHub UI clicks, add fork guidance
- Spell out the land-on-main-first step and branch dropdown

## [0.2.3] - 2026-06-13

### CI

- Automate releases via prepare-PR + publish-on-merge (Lane B)

## [0.2.2] - 2026-06-13

### Documentation

- *(research)* Verify ollama passthrough findings; amend with §6 addendum

### Features

- *(config)* Make ollama stream/thinking/output_limit configurable; consolidate docs
- *(config)* Restructure ollama runtime knobs from verified passthrough

### Testing

- *(slow)* Live e2e tests for the [[npm]] plugin + ADOS permutations
- *(slow)* Pin Ollama /v1 contract (reasoning_effort, think, num_ctx)
- *(slow)* In-sandbox wire capture of the opencode->ollama contract

## [0.2.0] - 2026-06-12

### Bug Fixes

- *(install)* Mount agent home at create so it agrees with sandbox start
- *(install)* Honor cwd/env in advise() and clone generic tools to a temp dir

### Features

- *(sandbox)* Durable per-key agent home, cwd -w fix, name registry
- *(config)* Add [[npm]] OpenCode plugin lane

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

