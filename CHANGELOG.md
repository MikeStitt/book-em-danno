# Changelog

All notable changes to `danno` (book-em-danno). Generated from conventional
commits by [git-cliff](https://git-cliff.org).

## [0.16.1] - 2026-07-16

### Bug Fixes

- *(exec)* Isolate captured subprocess stdin from the terminal

## [0.16.0] - 2026-07-16

### Bug Fixes

- *(bench)* Reap the in-VM harness after a runaway-gate kill
- *(bench)* Count Gate-1 rounds by request path, not usage presence (F1)
- *(exec)* Reap the process tree and surface reader errors on gate kill (F2/F3)
- *(bench)* Own the --no-save-captures temp dir; reject --capture-dir conflict (F4)
- *(config)* Drop unreachable plannotator installer from the example
- *(stubai)* Skip getfqdn in server_bind to unhang macOS CI
- *(stubai)* Record response before sending body to close transcript race

### Documentation

- *(bench)* Plan long-lived validation scripts for the runaway gates
- *(bench)* Record the PR #88 runaway-gates review findings
- *(bench)* Detail why each PR #88 finding matters and its fix design
- *(bench)* Record GV1 done + Q1 resolution in the validation plan
- *(readme)* Highlight the unit vs end-to-end pytest tiers
- *(bench)* Mark GV2/GV3 fixtures live-verified after green slow run

### Features

- *(bench)* Record runaway-gate observability on each bench.json row
- *(bench)* --no-save-captures writes nothing to disk

### Refactor

- *(telemetry)* Dedupe usage extraction onto capture/usage
- *(bench)* HarnessName alias for the name set; document always-on capture (F8/F6)

### Testing

- *(bench)* GV0 stub AI + gate-sensor/watchdog validation scripts
- *(bench)* Author Tier B gate-validation suites GV2/GV3 (not live-verified)
- *(bench)* Fix GV2/GV3 findings from first live run
- *(bench)* Make GV2/GV3 termination matrix green against real sandboxes
- *(slow)* Make sandbox e2e tests sbx-only; drop plannotator from npm live test

## [0.15.1] - 2026-07-13

### Bug Fixes

- *(telemetry)* Parse OpenAI Responses-API captures (usage + transcript)

### Documentation

- *(bench)* Design runaway gates + capture-always-on for danno bench
- *(bench)* Record runaway-gates implementation status

### Features

- *(bench)* Add runaway-gate config schema and resolution
- *(bench)* Add runaway-gate sensor and watchdog mechanism
- *(bench)* Enforce runaway gates per cell via the watchdog
- *(bench)* Make capture always-on; add --no-save-captures
- *(bench)* Native polite-stop caps (grace margin) + gates provenance

## [0.15.0] - 2026-07-13

### Bug Fixes

- *(sandbox)* Handle sbx-specific create/rm/ls behaviors found live
- *(sandbox)* Restore sbx egress isolation — allow only the Ollama endpoint
- *(sandbox)* Resolve local Ollama to 127.0.0.1 loopback, not an auto-detected LAN IP
- *(claurst)* Wait out the shell VM's boot-apt lock before installing ALSA
- *(claurst)* Retry apt through the boot-apt lock race (fuser-wait was flaky)

### Documentation

- *(claurst)* Mark fix/ollama-nvidia-stream-usage binary-clean
- *(claurst)* Bug 8/9 written, Bugs 4/5 re-parented, add Bug 10
- *(claurst)* Auto-compaction findings — real bug vs enhancements
- Plan migration from `docker sandbox` to `sbx` (dual-CLI)
- Record D1 (default to sbx) in the sbx-migration plan
- Mark sbx migration shipped (P1-P5) + document the DANNO_SANDBOX_CLI knob
- Record verified Phase-2 mechanism findings + the relay-based plan
- Sbx egress model + local-Ollama reachability, for independent review
- Sbx HAS the host.docker.internal rewrite — correct model + Phase-2 plan
- *(readme)* Split the network model by sandbox backend (sbx vs docker)
- *(sbx)* Record W1+W2 done — all four harnesses E2E-verified under sbx
- *(sbx)* Verify relay-free claurst timeout ceiling (W5); record W4 deferral
- *(sbx)* Mark W7 done — backend-aware deny detection is a documented guardrail

### Features

- *(sandbox)* Route the sandbox CLI through a backend seam, default to sbx
- *(sandbox)* Declared [sandbox] knobs for the sbx workarounds (cli, resolve_ollama_host)
- *(doctor)* Recommend loopback-only Ollama; WARN on 0.0.0.0 (plan S3)
- *(claurst)* Drop the in-VM Ollama relay — dial host Ollama directly (plan W3)
- *(claurst)* Relay-free --capture — dial the recording proxy directly (plan W6)

### Refactor

- *(sandbox)* Retire the sbx loopback-resolver workaround (plan W1)

### Testing

- *(portability)* Teach the probe sbx + bash-kind + sbx --help capture
- *(portability)* Consolidate cmd/powershell/wsl probe reports

## [0.14.0] - 2026-07-11

### Documentation

- *(config)* DoR for danno.toml harness overrides + model-level limits

### Features

- *(config)* Per-harness override escape hatch + model-level limits

## [0.13.0] - 2026-07-10

### Bug Fixes

- *(bench)* Warm models just-in-time so multi-model matrices survive VRAM eviction

### Documentation

- Note pre-warm + load-timing plot in aider cross-harness record
- Record SWE-bench + Aider grading-fidelity investigation
- Plan for testing danno on Windows (WSL2, cmd, PowerShell)
- Broaden Windows test plan to cross-platform (add Linux + CI matrix)
- Fill in cross-platform plan body (two-tier content)
- *(upstream)* Restructure claurst PR drafts as a dependency graph, not independent fixes

### Features

- *(bench)* Pre-warm local models + plot first-call load latency

### Testing

- *(portability)* Add cross-platform danno probe + run instructions

## [0.12.0] - 2026-07-08

### Bug Fixes

- *(swebench)* Give the model its real checkout path + a python shim
- *(swebench)* Install python shim in ~/.local/bin (agent-writable, on PATH)
- *(swebench)* Retry transient datasets-server 502s instead of aborting

### Documentation

- Record SWE-bench grader findings; clarify README harnesses + commands
- Audit occ fork prompt-divergence + Piebald prompt reprocessors
- Frame danno's unit-of-test as the triple in README intro
- Archive NVIDIA NIM free-tier probe (setup, gating, cost, turn-cap)
- Record cross-harness 3-task aider runs + resolve claude default model
- Fold claude 4-model sweep into report; fix stale bench help text

### Features

- *(bench)* Sweep claude harness across declared inert-backend models

## [0.11.1] - 2026-07-07

### Documentation

- DoR for renaming outer coding-tool concept agent → harness

### Refactor

- Rename outer coding-tool concept `agent` → `harness`

## [0.11.0] - 2026-07-06

### Bug Fixes

- *(bench)* Seed opencode.jsonc so opencode resolves local models
- *(bench)* Route occ/claurst to local Ollama on non-`ollama` backend names

### Features

- Unified [env] mechanism + occ as a first-class agent
- *(occ)* Fork for long/slow local loops + 4-agent bench eval
- *(bench)* Capture & report green+yellow telemetry
- *(bench)* Per-model cloud auth + multi-agent sweeps

## [0.10.0] - 2026-06-28

### Bug Fixes

- *(sandbox)* Make claurst install survive proxy-truncated downloads
- *(config)* Drop reference to non-existent 'danno config generate'
- *(validator)* Drive cloud claurst models direct, skip the Ollama relay

### Documentation

- Investigate claurst + qwen3-coder-next narrate-then-stop
- Record captured claurst request (narrate-then-stop is model-side)
- *(claurst)* Record claurst integration findings and multi-model spike
- *(claurst)* Add fork + first-class-integration plan
- *(upstream)* Draft bug reports + PR text for the 6 claurst fork fixes
- *(claurst)* Describe claurst as a first-class danno agent (local + cloud)

### Features

- *(capture)* Record claurst<->Ollama wire traffic via the relay
- *(relay)* Opt-in flushed both-ends tracing via DANNO_RELAY_LOG
- *(config)* Generate claurst model-registry overlay from danno.toml
- *(config)* Map danno [agents] to claurst agent definitions (Part 3 / M2)
- *(sandbox)* Emit + consume claurst config from danno.toml (Part 3 / wiring)
- *(sandbox)* Claurst cloud lift — reach NVIDIA NIM through the egress proxy (Part 3 / Layer 2)
- *(claurst)* Ship the danno fork build (v0.1.6-danno1) + stamp-gated skip
- *(benchmark)* Reject non-opencode agents loud (claurst is a bench/validate AUT)

## [0.8.0] - 2026-06-24

### Documentation

- Add claude/claurst use cases and correct the command surface

### Features

- *(sandbox)* Run claurst as an interactive --agent on local Ollama
- Document sandboxed AI process

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

