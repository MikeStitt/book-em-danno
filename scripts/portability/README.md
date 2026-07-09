# danno portability probe

`probe.py` runs danno's **side-effect-free** surfaces in the current shell/OS, records
environment facts, preflights the run-leg prerequisites, and writes a JSON + Markdown
report. It exists to execute **Tier 1** of
[`../../.docs/plan-test-danno-cross-platform.md`](../../.docs/plan-test-danno-cross-platform.md)
with minimal typing — the same one command in every shell.

**What it does:** `danno --help`, `danno doctor`, `danno install --help` (+ `--dry-run`
if a `danno.toml` is in the cwd); detects OS / shell / arch / Python and `which`
danno·uv·docker·git·bash·ollama; flags **Git Bash presence** (the H1-masking signal);
and preflights the run leg (`docker`, the `docker sandbox` subcommand, and Ollama
reachability). **What it does NOT do:** never `install --apply`, never a bench, no target
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
- **Run-leg preflight** — whether `docker`, the `docker sandbox` subcommand (the **R1/I5**
  signal — is it available on this OS?), and Ollama are all ready.
- **Hazard notes (Windows)** — whether **H1 is masked** (Git Bash on PATH → danno's host
  `bash` subprocess will succeed, so it's a *cmd+GitBash* result, **not** pristine cmd) and
  the **H4** secret-permissions caveat. Record these honestly when judging cmd/PowerShell.
