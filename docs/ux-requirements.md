# UX & Requirements Document: book-em-danno (Python Conversion)

## 1. Goals & Principles
- **Cross-Platform:** Unified experience across macOS, Linux (Unix), and Windows.
- **Non-Destructive:** Idempotent operations; preserve user files.
- **Declarative source of truth:** The user edits a single [`danno.toml`](#3-the-danno.toml-configuration-source-of-truth). The tool derives everything else (the OpenCode config, which tools to install, which backend each agent runs on) from it. Re-running converges.
- **Host stays clean:** The host project is not loaded up with framework assets by default. Agentic tools (ADOS, plannotator, opencode-planner) install per-tool either **into the Docker sandbox** or **project-local**, as each tool declares in `danno.toml`. ADOS is **one tool in the catalog**, not the reason the toolkit exists.
- **Transparent automation:** The tool guides the user to a working containerized OpenCode every time. It auto-owns the config files it generates, but it **never runs hidden one-liners** — host, Ollama, and Docker actions are shown as copy-paste commands so the user stays oriented and can search for help when something breaks. See [§5 Automation Policy](#5-automation-policy-automatic-vs-advised).
- **Progress Tracking:** Implementation progress is tracked in [docs/conversion-plan.md](conversion-plan.md).

## 2. Command Surface (UX)
The tool is distributed as a Python package `book-em-danno`, exposing the `danno` (and `book-em-danno`) CLI.

### End-to-End User Story: "From Zero to AI Coding"
This story defines the primary path for a developer setting up an OpenCode-powered project using a hybrid model runtime.

| Step | Goal | Command | Expected Tool Response | System State After Step |
| :--- | :--- | :--- | :--- | :--- |
| 1 | **Declare** | (edit `danno.toml`) | The user fills in backends, the agent→model map, `default_agent`, and the tool catalog. | `danno.toml` is the source of truth. |
| 2 | **Provision** | `danno install --target .` | `[INFO] Reading danno.toml...`<br>`[INFO] Generating OpenCode config...`<br>`[SUCCESS] Wrote .opencode/opencode.jsonc.` | Project contains `.opencode/opencode.jsonc` (auto-generated on first run). |
| 3 | **Provision** | `danno doctor` | `PASS: Python 3.13`<br>`FAIL: Ollama server reachable`<br>`fix: OLLAMA_KEEP_ALIVE=30m ollama serve` | User knows they must start the local LLM engine before proceeding. |
| 4 | **Isolate** | `danno sandbox start` | `[INFO] The following will create the sandbox:`<br>`  docker sandbox create ...`<br>`Run it yourself, or re-run with --apply.` | A copy-paste command (or, with `--apply`, a Linux microVM) wired to host Ollama with the project mounted. |
| 5 | **Equip** | `danno tools install` | `[INFO] ados -> sandbox`<br>`[INFO] opencode-planner -> sandbox`<br>`[INFO] plannotator -> project`<br>(shows the commands; runs with `--apply`) | The declared agentic tools are placed in their per-tool targets. |
| 6 | **Operate** | (Inside TUI) | `OpenCode TUI launches.`<br>`User interacts with installed agents (e.g., @pm).` | Developer is coding with AI assistance in a secure, isolated environment. |
| 7 | **Debug** | `danno sandbox shell` | `[INFO] Connecting to bash...`<br>`root@sandbox:/work#` | User has direct terminal access to the VM for system-level debugging. |
| 8 | **Tune** | (edit `danno.toml`, then `danno config generate`) | `[INFO] Re-reading danno.toml...`<br>`--- a/.opencode/opencode.jsonc`<br>`+++ proposed`<br>`Apply? re-run with --apply` | On a subsequent run the tool shows a **diff for approval**, never a silent overwrite. |

### Core Entry Point: `danno` (alias for `book-em-danno`)

#### Root Commands
| Command | Description | User Input Example | Expected Tool Response |
| :--- | :--- | :--- | :--- |
| `install` | Read `danno.toml` and write the OpenCode config into a target project. | `danno install --target ./my-project` | - Validates target directory and `danno.toml`.<br>- Generates `.opencode/opencode.jsonc` from the agent→model map (first run writes; later runs propose a diff).<br>- **Result:** `[SUCCESS] OpenCode configured in /path/to/project.` |
| `config` | Generate/Update the OpenCode config from `danno.toml`. | `danno config generate --profile hybrid` | - Produces `.opencode/opencode.jsonc` from `danno.toml` (the auto-loaded file — see note below).<br>- First run writes; subsequent runs show a diff and require `--apply`.<br>- **Result:** `.opencode/opencode.jsonc` created/updated. |
| `tools` | List or install the agentic tool catalog declared in `danno.toml`. | `danno tools install` | - `tools list` shows each tool, source, and `install_to` target.<br>- `tools install` shows the per-tool install commands (sandbox or project), running them only with `--apply`. |
| `ollama` | Tooling to manage Ollama models and server state. | `danno ollama pull gemma3:27b` | - Checks if Ollama is running.<br>- If not, advises (copy-paste): `"Ollama is not running. Run in a new terminal: 'OLLAMA_KEEP_ALIVE=30m ollama serve'"`<br>- Docker alternative: `"Alternatively: 'docker run -d -v ollama:/root/.ollama -p 11434:11434 --name ollama ollama/ollama'"`<br>- Pull commands are shown; executed only with `--apply`. |
| `doctor` | Diagnostics check for the environment. | `danno doctor` | - Checks Python (3.13), Git, and OpenCode installation.<br>- Verifies Ollama connectivity and model existence.<br>- Checks Docker Sandbox capabilities.<br>- **Result:** A checklist of PASS/FAIL with copy-pasteable remediation commands. |
| `sandbox` | Manage the isolated dev environment. | `danno sandbox start` | - **Create**: shows (or with `--apply` runs) `docker sandbox create`, explaining what it does.<br>- **Rebuild/Restart**: after `danno.toml` edits, shows the rebuild + restart commands.<br>- **Update**: shows how to update OpenCode inside the container.<br>- **Shell**: `danno sandbox shell` connects to a bash terminal in the VM.<br>- **Config**: `danno sandbox config` updates sandbox/container settings (distinct from `config generate`, which writes the model map).<br>- **Guidance**: if network fails, advises: `"Your Ollama is bound to 127.0.0.1. Restart it with 'OLLAMA_HOST=0.0.0.0:11434 ollama serve' so the sandbox can reach it."` |

> **Note — which file is generated.** OpenCode only **auto-loads** `.opencode/opencode.jsonc` (and the project-root `opencode.jsonc`); `opencode-*.jsonc` variants need the `OPENCODE_CONFIG` env var and would silently not load. Therefore both `install` and `config generate` write the single auto-loaded **`.opencode/opencode.jsonc`**.

### Global Flags
- `--apply`: Execute the host/Docker/Ollama commands the tool would otherwise only print. Without it, those actions are **advise-only** (copy-paste). Config-file generation is unaffected (see [§5](#5-automation-policy-automatic-vs-advised)).
- `--config <path>`: Path to the `danno.toml` source of truth (default: `./danno.toml`).
- `--dry-run` / `-n`: Log intended actions and config diffs without writing or executing anything.
- `--verbose` / `-v`: Show debug logs (equivalent to `VERBOSE=true`).
- `--help` / `-h`: Contextual help for the specific command.

## 3. The `danno.toml` Configuration (Source of Truth)

The user edits **`danno.toml`**; the tool reads it and derives the OpenCode config and tool installs. It declares **backends** (where models run), **models** (a backend + a tag/id), the **agent→model map**, the **default agent**, and the **tool catalog**.

```toml
# danno.toml — declarative source of truth for book-em-danno

[project]
target = "."                       # project to configure

[defaults]
default_agent = "pm"               # OpenCode default_agent
profile       = "hybrid"           # hybrid | cloud-only | local-only (preset; per-agent entries win)

# --- Backends: where models run. kind selects the runtime. ---
[backends.ollama]                  # IMPLEMENTED
kind     = "ollama"
base_url = "http://host.docker.internal:11434/v1"   # Docker host gateway; use localhost:11434/v1 for host-only runs
num_ctx  = 32000

[backends.cloud]                   # IMPLEMENTED
kind     = "cloud"
provider = "anthropic"             # API key stays in the env / OpenCode provider — never written to config

[backends.llamacpp]                # STUBBED (schema present; generator raises "not yet implemented")
kind     = "llamacpp"
base_url = "http://host.docker.internal:8080/v1"     # served via llama.cpp's OpenAI-compatible llama-server

# --- Models: a named (backend, tag/id) pair. ---
[models.gemma]
backend   = "ollama"
tag       = "gemma3:27b"           # local models MUST support tool-calling (gemma3:1b does NOT)
tool_call = true

[models.sonnet]
backend = "cloud"
id      = "anthropic/claude-sonnet-4-6"

# --- Agent -> model. Unlisted agents fall back to the profile's tier default. ---
[agents]
# high-stakes / core -> cloud
architect = "sonnet"
reviewer  = "sonnet"
pm        = "sonnet"
coder     = "sonnet"
# well-scoped / cheap -> local
committer           = "gemma"
runner              = "gemma"
external-researcher = "gemma"

# --- Tool catalog: each tool installs per-tool into the sandbox or project-local. ---
[[tools]]
name       = "ados"
source     = "https://github.com/juliusz-cwiakalski/agentic-delivery-os"
install_to = "sandbox"             # sandbox | project

[[tools]]
name       = "opencode-planner"
source     = "https://github.com/timrichardson/opencode-planner"
install_to = "sandbox"

[[tools]]
name       = "plannotator"
source     = "https://plannotator.ai/"
install_to = "project"             # NOTE: plannotator is a hosted/web tool; its exact install mechanism is a TODO
```

### Backend abstraction
- **ollama** — local models via an OpenCode `@ai-sdk/openai-compatible` provider; referenced as `ollama/<tag>`. **Implemented now.**
- **cloud** — whatever provider the developer has configured in OpenCode; keys stay `{env:...}`, never written to the committed config. **Implemented now.**
- **llamacpp** — local models served by llama.cpp's OpenAI-compatible `llama-server`. **Stubbed**: the schema slot exists so `danno.toml` can declare it, but the generator raises a clear "llama.cpp backend not yet implemented" until built. The abstraction is designed for all three from the start.

### Tool catalog
- `danno tools list` prints each catalog entry (`name`, `source`, `install_to`).
- `danno tools install` resolves each tool's install commands and shows them; with `--apply` it runs them. `install_to = "sandbox"` installs into the Docker sandbox image/container; `install_to = "project"` installs project-local.
- **Per-tool install mechanisms differ** and are captured as TODOs where unknown: ADOS and opencode-planner are git repos (clone/install); plannotator is a hosted/web tool whose local footprint must be confirmed.

## 4. OS-Specific Command Strings (Implementation Guidance)

The tool maintains a mapping of "Desired Action" → "OS-Compatible String", surfaced as copy-paste (run automatically only under `--apply`).

#### Docker Sandbox Runtime
| Component | Requirement / Behavior | Implementation Note |
| :--- | :--- | :--- |
| VM Creation | `docker sandbox create` | Must mount the target project at its host path. |
| Egress Proxy | `--policy allow` + `--allow-host localhost:11434` | Necessary to reach host Ollama through the VM's proxy. |
| Environment | Temp file injection (`--env-file`) | Used to pass `OLLAMA_BASE_URL` and API keys securely. |
| Connectivity | Host Ollama bound to `0.0.0.0` | Tool must warn if `lsof` detects loopback-only binding. |
| Rebuild/Restart | Shown after `danno.toml` edits that change the sandbox | Copy-paste rebuild + restart; run with `--apply`. |
| Update OpenCode | Shown command to update OpenCode inside the container | Keeps the containerized OpenCode current. |

#### macOS/Windows Support Note
The tool supports macOS and Windows by utilizing the Docker Sandbox runtime (above) to provide a Linux-based development environment with consistent behavior across platforms. The "macOS Native Sandbox" (Seatbelt) approach is out of scope.

## 5. Automation Policy (Automatic vs. Advised)

The defining UX decision: **guide the user to a working containerized OpenCode every time, without ever running hidden one-liners.** Two tiers:

**Tier 1 — Files the tool owns (automatic, with a safety rail):**
- `.opencode/opencode.jsonc` is **auto-generated on first run** (nothing to clobber).
- On a **subsequent** run where the generated content differs, the tool prints a **diff and requires approval** (`--apply`); it never silently overwrites a hand-edited config. Identical content is a no-op.

**Tier 2 — Host, Ollama, and Docker actions (advised by default):**
- Starting Ollama, pulling models, creating/rebuilding/restarting the sandbox, and updating OpenCode in the container are shown as **copy-paste commands by default**.
- A dry-run **always shows the literal commands first**; the opt-in `--apply` flag runs exactly those shown commands.
- Rationale: if the tool silently ran every one-liner, the user wouldn't know what happened or how to search for help when it broke. Showing the command keeps them oriented.

## 6. Requirements Logic
- **Requirements Gathering:**
    - Must replace `scripts/lib/common.sh` logging and command-execution logic (`log_*`, `run_cmd`, dry-run gating) with a Python equivalent.
    - Must handle filesystem operations across OS (use `pathlib`).
    - Must parse `danno.toml` (via `tomllib`) and validate it at the boundary, failing loud on bad input.
- **Success Criteria:**
    - Editing `danno.toml` and re-running converges to the same `.opencode/opencode.jsonc` (idempotent).
    - All `danno` commands reach the same final state as their `.sh` predecessors, with no "bash not found" / path errors on Windows.
    - `danno doctor` correctly identifies missing dependencies and suggests a fix.
    - Tier-2 actions never execute without `--apply`.
