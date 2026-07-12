# danno portability probe

`probe.py` runs danno's **side-effect-free** surfaces in the current shell/OS, records
environment facts, preflights the run-leg prerequisites, and writes a JSON + Markdown
report. It exists to execute **Tier 1** of
[`../../.docs/plan-test-danno-cross-platform.md`](../../.docs/plan-test-danno-cross-platform.md)
with minimal typing — the same one command in every shell.

**What it does:** `danno --help`, `danno doctor`, `danno install --help` (+ `--dry-run`
if a `danno.toml` is in the cwd); detects OS / shell / arch / Python and `which`
danno·uv·docker·sbx·git·bash·ollama; **classifies `bash`** (git-bash vs WSL-shim vs
absent — the H1 signal); preflights the run leg (the active sandbox CLI — **`sbx`** or the
deprecated **`docker sandbox`** — plus Ollama reachability); and, when `sbx` is present,
**captures `sbx {version,create,exec,policy,secret} --help`** for the sbx-migration
investigation. **What it does NOT do:** never `install --apply`, never a bench, no target
repo is touched. The run leg itself (P3–P5) stays manual per the plan.

## Run it (identical command in every shell)

One-time setup on each box:

```
git clone https://github.com/MikeStitt/book-em-danno.git
cd book-em-danno
git checkout docs-plan-test-danno-on-windows
uv sync
```

Then run — **only the `--shell` label changes** (forward slashes work on Windows too):

| shell | command |
|---|---|
| **cmd** | `uv run python scripts/portability/probe.py --shell cmd --ollama-host http://10.0.1.27:11434` |
| **PowerShell** | `uv run python scripts/portability/probe.py --shell powershell --ollama-host http://10.0.1.27:11434` |
| **WSL** | `uv run python scripts/portability/probe.py --shell wsl --ollama-host http://10.0.1.27:11434` |
| **Ubuntu** | `uv run python scripts/portability/probe.py --shell ubuntu --ollama-host http://10.0.1.27:11434` |

`--ollama-host` is your Mac's LAN URL (see the firewall/bind steps in the plan). You can
instead export it once and drop the flag:

- cmd: `set DANNO_PROBE_OLLAMA=http://10.0.1.27:11434`
- PowerShell: `$env:DANNO_PROBE_OLLAMA="http://10.0.1.27:11434"`
- bash/WSL/Ubuntu: `export DANNO_PROBE_OLLAMA=http://10.0.1.27:11434`

If `danno` isn't on PATH the probe falls back to `uv run danno`, so running it via
`uv run python …` (as above) always works after `uv sync`.

## Collect the results

Each run writes `scripts/portability/reports/report-<os>-<shell>-<py>.{json,md}`. To
pool all four environments for central synthesis, commit the reports from each box:

```
git add scripts/portability/reports/report-*.md scripts/portability/reports/report-*.json
git commit -m "test(portability): report from <env>"
git push
```

(Or just paste the `.md` back.) With all four reports on the branch, the plan's Tier-1
matrix + fix backlog can be filled in one place.

## Reading a report

- **Surfaces table** — `✅/❌` + exit code + first error line per danno command.
- **Run-leg preflight** — the **active sandbox CLI** (`sbx` preferred, else the deprecated
  `docker sandbox`; the **R1/I5** signal), docker, and Ollama.
- **sbx help capture** — the raw `sbx … --help` outputs (only when `sbx` is installed) that
  feed the sbx-migration plan's investigations (exec `--env-file`/`-it`, `policy allow`
  syntax, `create` blueprints, `secret` model).
- **Hazard notes (Windows)** — the **H1** classification: `git-bash` (found, but path/docker
  interop unverified), **`wsl-shim`** (⚠️ `bash` is `System32\bash.exe` → danno's host bash
  would run *inside WSL*), or absent (H1 fires); plus the **H4** secret-permissions caveat.
