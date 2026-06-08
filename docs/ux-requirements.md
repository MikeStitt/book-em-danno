# UX & Requirements Document: ADOS Toolkit (Python Conversion)

## 1. Goals & Principles
- **Cross-Platform:** Unified experience across macOS, Linux (Unix), and Windows.
- **Non-Destructive:** Idempotent operations; preserve user files.
- **Infrastructure Lite:** No mandatory local installation of ADOS (`ados` repo); move toward asset management over script execution.
- **Guided Execution:** Provide clear command strings for Docker/Ollama when host-level installation is avoided.

## 2. Command Surface (UX)
The tool will be distributed as a Python package `book-em-danno`.

### End-to-End User Story: "From Zero to AI Coding"
This story defines the primary path for a developer setting up an ADOS-powered project using a hybrid model runtime.

| Step | Goal | Command | Expected Tool Response | System State After Step |
| :--- | :--- | :--- | :--- | :--- |
| 1 | **Provision** | `danno install --target .` | `[INFO] Copying ADOS assets...`<br>`[INFO] Generating hybrid config...`<br>`[SUCCESS] ADOS and OpenCode configured.` | Project contains `.opencode/agent`, `.opencode/command`, and `opencode.jsonc`. |
| 2 | **Provision** | `danno doctor` | `PASS: Python 3.13`<br>`FAIL: Ollama server reachable`<br>`fix: run 'ollama serve'` | User knows they must start the local LLM engine before proceeding. |
| 3 | **Isolate** | `danno sandbox start` | `[INFO] Creating Docker Sandbox...`<br>`[INFO] Configuring network proxy...`<br>`[INFO] Launching OpenCode TUI...` | A Linux microVM exists; it has the project mounted and is wired to host Ollama. |
| 4 | **Operate** | (Inside TUI) | `OpenCode TUI launches.`<br>`User interacts with ADOS agents (e.g., @pm).` | Developer is actively coding with AI assistance in a secure, isolated environment. |
| 5 | **Debug** | `danno sandbox shell` | `[INFO] Connecting to bash...`<br>`root@sandbox:/work#` | User has direct terminal access to the VM for system-level debugging. |
| 6 | **Tune** | `danno config generate --local-only` | `[INFO] Updating laocal model map...`<br>`[SUCCESS] Configuration updated.`<br>`Note: Restart sandbox for changes to take effect.` | The AI's behavior has changed; high-stakes agents now run on the local Gemma model. |

### Core Entry Point: `danno` (alias for `book-em-danno`)

#### Root Commands
| Command | Description | User Input Example | Expected Tool Response |
| :--- | :--- | :--- | :--- |
| `install` | Install ADOS assets and OpenCode config into a target project. | `danno install --target ./my-project` |- Validates target directory.<br>- Detects source repository root (checked against `.opencode/agent`).<br>- Copies assets (idempotent).<br>- Generates initial `.opencode/opencode.jsonc` pointing to local Ollama or Cloud AI based on environment.<br>- Writes provenance file (`.opencode/ados-provenance.txt`).<br>- **Result:** `[SUCCESS] ADOS and OpenCode configured in /path/to/project.` |
| `ollama` | Tooling to manage Ollama models and server state. | `danno ollama pull gemma2` | - Checks if Ollama is running.<br>- If not, provides explicit guidance: `"Ollama is not running. Please run the following command in a new terminal: 'ollama serve'"`<br>- Provides Docker alternative for non-native install: `"Alternatively, run: 'docker run -d -v ollama:/root/.ollama -p 11434:11434 --name ollama ollama/ollama'"`<br>- Executes pull if local.` |
| `doctor` | Diagnostics check for the environment. | `danno doctor` | - Checks Python (3.13), Git, and OpenCode installation.<br>- Verifies Ollama connectivity and model existence.<br>- Checks Docker Sandbox capabilities.<br>- **Result:** A checklist of PASS/FAIL with copy-pasteable remediation commands. |
| `sandbox` | Manage the isolated dev environment. | `danno sandbox start` | - **Build**: If VM doesn't exist, creates it via `docker sandbox create` (explains what this does).<br>- **Run**: Launches OpenCode inside the box with injected env vars.<br>- **Shell**: Via `danno sandbox shell`, connects to a bash terminal in the VM.<br>- **Config**: Via `danno sandbox config`, updates settings for an existing sandbox without rebuilding.<br>- **Guidance**: If network fails, suggests: `"Your Ollama is bound to 127.0.0.1. Please restart it with 'OLLAMA_HOST=0.0.0.0:11434 ollama serve' so the sandbox can reach it."` |
| `config` | Generate/Update `.opencode` configuration files. | `danno config generate --profile hybrid` | - Produces JSONC based on model mappings (hybrid, cloud-only, local-only).<br>- **Result:** `.opencode/opencode-models.jsonc` created/updated. |



### Global Flags
- `--dry-run` / `-n`: Log intended actions without executing.
- `--verbose` / `-v`: Show debug logs (equivalent to `VERBOSE=true`).
- `--help` / `-h`: Contextual help for the specific command.

### 3. OS-Specific Command Strings (Implementation Guidance)

The tool will maintain a mapping of "Desired Action" $\rightarrow$ "OS-Compatible String".

#### Docker Sandbox Runtime
| Component | Requirement / Behavior | Implementation Note |
| :--- | :--- | :--- |
| VM Creation | `docker sandbox create` | Must mount the target project at its host path. |
| Egress Proxy | `--policy allow` + `--allow-host localhost:11434` | Necessary to reach host Ollama through the VM's proxy. |
| Environment | Temp file injection (`--env-file`) | Used to pass `OLLAMA_BASE_URL` and API keys securely. |
| Connectivity | Host Ollama bound to `0.0.0.0` | Tool must warn if `lsof` detects loopback-only binding. |

#### macOS Native Sandbox (Experimental)
| Component | Requirement / Behavior | Implementation Note |
| :--- | :--- | :--- |
| Confinement | `sandbox-exec -f <profile>` | Use Seatbelt to restrict WRITES to target + caches. |
| Profile | Generated dynamically | Must include `/private/tmp`, `${HOME}/.config/opencode`, etc. |


## 4. Requirements Logic
- **Requirements Gathering:**
    - Must replace `scripts/lib/common.sh` logging and command execution logic.
    - Must handle filesystem operations across OS (use `pathlib`).
    - Must provide a way to find the ADOS source repository without forcing it into the PATH.
- **Success Criteria:**
    - All `danno` commands result in the same final state as their `.sh` predecessors.
    - No "bash not found" or path errors on Windows.
    - `danno doctor` correctly identifies missing dependencies and suggests a Docker fix.
