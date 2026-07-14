# Live-verifying the `danno bench` runaway gates (in the Docker sandbox)

How to reproduce the full in-sandbox verification of the runaway gates
(`.docs/plan-bench-runaway-gates.md`) — a real `danno bench` run where a gate
fires against opencode running inside the Docker Desktop microVM, plus the
focused **in-VM reap** check that motivated a fix.

**First done:** 2026-07-14 (Claude, Opus 4.8), macOS + Docker Desktop 29.4.2,
`sbx` v0.12.0, Ollama with `qwen3.6:27b-q4_K_M`. It surfaced (and fixed) a real
defect — see §3.

---

## 0. Prerequisites

- **Docker Desktop** running, with the sandbox CLI (`sbx` — `danno` shells to it;
  check `sandbox_cli.base()`). `sbx` v0.12.0 verified.
- **Ollama** running on the host (`curl -s localhost:11434/api/tags`) with a
  **tool-capable** model pulled. `gemma3:1b` cannot tool-call; use e.g.
  `qwen3.6:27b-q4_K_M` (slow but reliable) or a smaller tool-capable model.
- **danno** installed (`uv run danno --version`).

## 1. The throwaway target (forces a gate to fire)

Create a scratch target dir with a `danno.toml` + `benchmarks.toml`. The trick is
to set a gate **far below** what a real turn needs, so it fires deterministically.
Gate 3 (`timeout_s`) is best for this: it has no harness-side polite-stop, so it
directly exercises the **external kill + reap** path.

`/tmp/danno-gate-live/danno.toml`:

```toml
[backends.ollama]
kind = "ollama"
base_url = "http://host.docker.internal:11434/v1"

[models.qwen]
backend = "ollama"
tag = "qwen3.6:27b-q4_K_M"     # any tool-capable local model
context_budget = 32768
output_limit = 4096

[agents]
build = "ollama/qwen"
```

`/tmp/danno-gate-live/benchmarks.toml`:

```toml
[aider_polyglot]
enabled = true
# Use a real polyglot-subset exercise. "python/anagram" is NOT in the subset and
# fails "exercise not found"; known-good ones: transpose / proverb / grade-school.
select = ["python/grade-school"]

[gates]
timeout_s = 30      # far below a real qwen-27B turn → Gate 3 fires
max_turns = 50      # (won't fire; leave loose so timeout is the sole trigger)
```

**To force Gate 1 (round cap) instead:** set `max_turns = 2` and `timeout_s = 600`.
opencode makes many rounds per exercise, so the watchdog kills at
`max_turns + grace` = `2 + max(3, 10%)` = **5** inference calls. (If opencode's
soft `agent.steps` actually honors 2, you'll instead see it stop *gracefully* at 2
with a normal verdict — also a valid outcome, proving the polite-stop.)

## 2. Run it

```bash
uv run danno bench --target /tmp/danno-gate-live --harness opencode \
    --keep-sandboxes --out /tmp/danno-gate-live/.bench
```

`--keep-sandboxes` leaves the VM up for inspection. The run: provisions the
sandbox, installs opencode into it, clones the polyglot benchmark, pre-warms the
model, then runs the one cell — which the watchdog kills at ~30 s.

**What to look for in the output:**

- `[INFO] warm  qwen3.6:27b-q4_K_M: loaded in …s` — model pre-warmed (outside the
  timed cell).
- `[INFO] reap harness processes in 'danno-bench-aider-…' after runaway-gate kill`
  followed by the `pkill` line — **the reaper firing** (the §3 fix).
- The summary row / `bench.json` verdict is **`timeout`** (or `runaway` for the
  Gate-1 variant), not a normal pass/stall.

**Confirm the recorded verdict** (`bench.json` is `{…, "results": [row, …]}`; each row has
`verdict` as a string and the breach in `error`):

```bash
python3 -c "import json; \
  [print(r['task'], r['verdict'], '| passed=', r['passed'], '| %.1fs'%r['latency_s'], '|', r['error']) \
   for r in json.load(open('/tmp/danno-gate-live/.bench/bench.json'))['results']]"
```

Observed 2026-07-14 (`qwen3.6:27b-q4_K_M`, `timeout_s=30`):
```
python/grade-school timeout | passed= False | 31.1s | runaway gate 'timeout': observed 30.1 exceeded limit 30.0
```
The watchdog killed opencode **mid–first inference call** (a cold 27B call > 30 s), so the
row's `wire.request_count` is `0` — Gate 3 fires on wall clock even before any `/v1`
response returns. `provenance.json` records the resolved `gates` block
(`{"max_turns":50,"max_tokens":2000000,"timeout_s":30.0,…}`).

## 3. The in-VM reap check (DoR §D3) — the defect this run found

**Question:** when the watchdog SIGKILLs the host-side `sbx exec`, does the
harness process *inside* the VM die?

**Answer: NO** — verified live. Killing the host `sbx exec` leaves the in-VM
process running. Reproduce it directly (no full bench needed), against any running
sandbox `$NAME`:

```python
import subprocess, time
NAME = "danno-bench-aider-182e3e85"   # any provisioned sandbox
def line():  # the orphan's ps line, excluding grep itself
    r = subprocess.run(["sbx","exec",NAME,"bash","-lc",
                        "ps -eo pid,args | grep 987654 | grep -v grep"],
                       capture_output=True, text=True, timeout=90)
    return r.stdout.strip() or "(none)"
# argv[0]='claurst' so it matches the reap pattern; 987654 makes it findable
proc = subprocess.Popen(["sbx","exec",NAME,"bash","-lc","exec -a claurst sleep 987654"])
time.sleep(9); print("before host kill:", line())
proc.kill(); proc.wait(); time.sleep(3)
print("after  host kill:", line(), "  <- SURVIVES: host kill does NOT reap the VM")
subprocess.run(["sbx","exec",NAME,"bash","-lc",
                "pkill -9 -f 'opencode|claurst|index\\.mjs|DANNO_RELAY' 2>/dev/null; true"])
time.sleep(2); print("after  reaper  :", line(), "  <- (none): the reaper works")
```

Observed 2026-07-14:
```
before host kill: 235 claurst 987654
after  host kill: 235 claurst 987654    <- host SIGKILL did NOT reap the VM
after  reaper    : (none)               <- REAP WORKS
```

**The fix (shipped in this branch):** `GateWatch.on_kill`, a best-effort reaper the
watchdog calls after a kill. `run_bench_task` passes `_reap_harness(runner, sandbox)`,
which runs `sbx exec <sandbox> pkill -9 -f 'opencode|claurst|index.mjs|DANNO_RELAY'`
— covering opencode, occ (`index.mjs`), claurst, and the occ in-VM Ollama relay
(`DANNO_RELAY…`). See `core/exec.py` (`GateWatch`, `_capture_watched`) and
`suites/base.py` (`_reap_harness`).

**Gotcha when verifying by hand:** `pgrep -af 987654` self-matches the checker's
own command line — use `ps … | grep … | grep -v grep`, or match the process's
`comm`/argv precisely, so you don't read a false "still alive".

## 4. Cleanup

```bash
sbx rm --force danno-bench-aider-<hash>     # the kept sandbox(es); `sbx ls` to find them
rm -rf /tmp/danno-gate-live
```

## 5. What this verifies vs. what it doesn't

- ✅ A gate fires against **real opencode in the real sandbox**, killing the cell and
  recording a loud gate verdict.
- ✅ The **reap** works: no orphaned harness process is left burning VM CPU.
- ✅ `provenance.json` records the resolved gates.
- ⚠️ Gate 1's **graceful-first** (option B) — a cap-honoring harness stopping itself
  *before* the kill — is best observed with occ/claurst (reliable `--max-turns`) at a
  low `max_turns`; opencode's `agent.steps` is advisory, so it may overshoot to the
  external kill (which is the point of the margin). Run the Gate-1 variant (§1) per
  harness to watch each.
