# Benchmark grading fidelity — SWE-bench & Aider: adopting official harness code

**Date compiled:** 2026-07-09 · **Status:** investigation / design record (no code
written). Captures a working session that started from "what are the known gaps in
`danno bench`?" and converged on a concrete standard + path space for making SWE-bench
and Aider Polyglot grading trustworthy and (where the benchmark allows) official-comparable.

**Companion docs:** [`aider-3task-cross-harness-runs.md`](aider-3task-cross-harness-runs.md),
[`claurst-qwen3-coder-next-investigation.md`](claurst-qwen3-coder-next-investigation.md) (§7),
[`nvidia-nim-free-tier-probe.md`](nvidia-nim-free-tier-probe.md),
[`bench-telemetry-features.md`](bench-telemetry-features.md). Memories:
`swebench-grader-nodeid-mismatch`, `danno-benchmarks-the-triple`,
`sandbox-egress-and-process-lifetime`.

---

## 0. The framing that organizes everything: three legs

Every `danno bench` task is really **three separable legs**, and keeping them separate
is what makes the rest of this document tractable:

| Leg | What it does | Whose code *can* own it |
|---|---|---|
| **seed** | provision the task (clone repo / write stub + tests, install deps) | danno **or** official data/specs |
| **run** | drive the agent loop (the harness-under-test + model editing code) | **danno only** — this is danno's entire reason to exist (swap opencode/claude/claurst/occ) |
| **grade** | score the result | danno **or** official harness |

**Key conclusion:** the *run* leg can never be the official harness (the official
harnesses drive *their own* agent, not our four). But the *seed* and *grade* legs are
exactly where the official projects have reusable code — and exactly where danno
currently reimplements worse versions. So "use more official harness code" = adopt on
**seed + grade**, keep **run** as danno's.

---

## 1. Known gaps in `danno bench` (the starting inventory)

### SWE-bench
1. **Grader node-id mismatch (danno's bug, open).** `SwebenchTask.grade`
   (`swebench.py:217-225`) runs a uniform `python3 -m pytest <FAIL_TO_PASS + PASS_TO_PASS>`.
   SWE-bench stores node ids in each repo's **native** runner format; only pytest-path
   repos (astropy) collect. **django** (unittest `method (module.Class.method)`) and
   **sympy** (bare `test_immutable`) **always grade FALSE even on the exact gold patch** —
   verified live 2026-07-07. Any "0/N swebench" is not a fair capability signal.
2. **We reimplemented grading instead of reusing upstream data** — the official
   `MAP_REPO_VERSION_TO_SPECS` per-repo eval specs are reusable *data* we should port.
3. **Live proxy-only pip provisioning** owns all the per-instance flakiness the official
   prebuilt images exist to remove (defensible, forced by egress, cost undersold).
4. **Signal is uneven across the matrix** — fine for cloud/Claude, context-noisy for
   small-local (astropy hit genuine 65,536 exhaustion). Not a "65k local" target.

### Cross-harness fairness
5. **`--max-turns` is harness-level and UNEQUAL** — occ=30, claurst≈10, opencode=uncapped.
   No bench-level flag. The runaway tail is structurally opencode's (383 tool_calls / 2314s).
6. **occ tool-call inflation + thin prompt** — occ is the latency tail; its one-sentence
   system prompt diverges from opencode's ~12K-token prompt. No fork enriches it (greenfield).
7. **Cost/token accounting is blind for local + occ** — local Ollama reports no usage
   (`tokens:0, cost:0`); occ reports 0 even on cloud; NVIDIA backend has no price table.
   Only the cloud Claude row carries real cost.

### Claude harness
8. **danno can't proxy/capture the Claude row** — Claude talks straight to
   `api.anthropic.com`; no `base_url` lever (`bench.py:243` warns). Partly mitigated by
   the `inert`-backend sweep (records model ids), but wire capture is impossible.
9. **n=1 per cell everywhere** — latency spreads are single samples; only cost is repeatable.

### NVIDIA NIM
10. **Free tier too gated to bench unattended; probe never run** — only 3 of ~11 small
    models callable; config staged in `bench2/*.nvidia-probe.toml`.

### Cross-cutting
11. **Every recorded aider row predates pre-warm** — the new pre-warm + load-timing plot
    (branch `bench-prewarm-and-load-timing`) removes the model-load confound *going
    forward*; historical qwen rows may embed a one-time cold-load hit. Warmed re-run pending.

**Highest signal-per-effort next steps identified:** (a) port per-repo SWE-bench grade
commands, (b) add `danno bench --max-turns N`, (c) warmed re-run of the 9 aider rows,
(d) wire `--capture`-derived token/cost into local + occ rows.

---

## 2. Aider suite — setup/grading problems (verified in code)

Read `aider.py`, `run.py`, `base.py`. Important nuance: **`provision` re-seeds solution
*and* test files before every variant** (`aider.py:93`, called per-variant at `run.py:69`),
which quietly prevents cross-variant *poisoning* but not the within-cell issues.

**Confirmed problems:**
1. **Test-file integrity unenforced.** The prompt says "Do not edit the test file(s)"
   (`aider.py:80`) but `grade()` (`aider.py:104-110`) runs whatever is on disk *after* the
   turn with no re-seed/hash check. A model that weakens/deletes assertions in its own cell
   **grades PASS falsely** (silent — violates fail-loud).
2. **Workspace never cleaned to pristine between variants.** `reset`/`provision` only
   overwrite the *listed* files (`mkdir(exist_ok=True)`); they never remove agent-created
   extras (`conftest.py`, helpers, `__pycache__`). On the default `shared` sandbox these
   survive and can alter a later variant's grade (pytest auto-loads `conftest.py`).
3. **`subdir = slug` drops the language** (`aider.py:156`) → `python/grep` and `go/grep`
   collide in one `grep/` dir. Latent today (python-only) but real if a matrix mixes langs.

**Comparability caveat:**
4. **Single attempt** (`base.py:124,133-158`) diverges from official Aider Polyglot's
   **two-attempt-with-test-feedback** protocol → danno pass-rates are systematically lower,
   not comparable to published aider numbers.

**Minor:**
5. Binary `-x -q` grade (`aider.py:41`) — no partial-credit / test-count; a collection
   error is indistinguishable from a logic failure in the recorded `passed: bool`.

**Fix for #1–#3 is one change:** seed each exercise into a pristine, **language-namespaced**
dir, **wipe** it before seeding (not `exist_ok` merge), and **re-assert the test file's
hash at grade time — fail loud if it changed.**

---

## 3. SWE-bench — adopting the official harness (Level A vs B)

The official `swebench` PyPI package **does not run an agent**; it consumes a *predictions
file* (one `model_patch` diff per instance, from any system) and grades it. That is exactly
the seam danno wants.

### Level A — full official grade (gold-standard)
danno owns seed + run; after the turn, `git diff` the VM checkout → `predictions.jsonl` →
run `python -m swebench.harness.run_evaluation` on the **host**. **Three distinct
environments** (this corrects the intuition that harness + tests would co-locate — that's
the *current* design; Level A *splits* them):

| # | Environment | Runs |
|---|---|---|
| 1 | **Host** | danno orchestration + official `swebench` package + Docker daemon that launches #3 |
| 2 | **danno sandbox** (microVM, proxy-only) — one per instance | the **run** leg (injected harness + model). No grading. |
| 3 | **Official grading container** (separate, one per instance) | official `eval.sh`: apply diff + test_patch, run real `test_cmd`, parse log. **No danno, no model.** |

Deletes our `grade()` entirely; sandbox provisioning flakiness stops affecting the *score*.
**Cost:** host needs Docker + the official images (pull GBs, or build with full egress) —
runs *outside* the microVM boundary (needs explicit sign-off).

### Level B — import specs + parsers (lighter)
Keep in-VM grading; replace uniform pytest with the official `MAP_REPO_VERSION_TO_SPECS`
(per-repo `test_cmd`) + official log parsers (`MAP_REPO_TO_PARSER`) reduced against
FAIL_TO_PASS/PASS_TO_PASS. No Docker/images. Directly kills the node-id bug.
**Verify:** whether `swebench.harness.{constants,log_parsers,grading}` imports without
dragging in `docker`/`datasets`; if not, vendor those modules (license headers, ADOS-style
provenance discipline).

### Aider asymmetry
Aider exercises are **self-grading** — there is no rich official grading *library* to
import (aider's grading ≈ what `aider.py:104` already does). The "official" value there is
**methodology** we'd *replicate*, not import: the 2-attempt loop + aider's exact
per-language test commands. Plus the §2 integrity fixes (ours regardless).

---

## 4. THE STANDARD (decided this session): respect each benchmark's test-visibility policy

**Official SWE-bench holds the grading tests OUT** — the agent sees `problem_statement`
only; the `test_patch` is applied *only at grade time* in a clean container. **Official
Aider feeds tests IN** (its 2-attempt protocol is built on test-output feedback).

**Standard adopted:** *danno must not show the agent the grading tests for benchmarks whose
policy is to withhold them.*

- **SWE-bench today is in the leakiest position by accident:** `provision` applies the
  `test_patch` *before* the run (`swebench.py:205`), so the agent can read/run the grading
  tests → self-verify → **answer-key leakage**, on top of the grader bug. Numbers recorded
  so far are non-comparable on **two** counts.
- **Aider is compliant** — its policy *is* test-visible, so seeding tests is correct.

### What the standard forces (shared by all SWE-bench paths)
- `provision`/`reset`: clone + checkout `base_commit` **only** — never `git apply
  <test_patch>` pre-run.
- `test_patch` becomes a **grade-time** concern, applied only where the agent never worked.
- Clarification: hold-out ≠ blind. At `base_commit` the repo's *pre-existing* tests are
  present; the agent can write/run its own tests. It just can't see the *added/modified*
  grading tests. That is the official condition.

### Surviving paths after the standard

| Path | Seed (run leg) | Grade leg | Leakage | Official-comparable | Home |
|---|---|---|---|---|---|
| **P1 — hold-out + Level B** | base only, danno VM | in-VM: apply test_patch post-run → official `test_cmd` + official Python log parser | none | "official-*ish*" (in-VM env; arm64 dep-drift) | ✅ ARM64 Mac (pure-Python instances) |
| **P2 — hold-out + Level A** | base only, danno VM | extract diff → official container applies test_patch | none | ✅ truly official | x86_64 Linux (native) |
| **P1 → P2 phased** | — | B default; A as opt-in `--official-grade` CI backend | none | both | Mac + CI |

**The standard KILLS:** (a) the current design (leaky + broken grader), and (b) the earlier
"position (c)" hybrid — *apply test_patch for self-verify, then exclude from the diff* — the
standard forbids showing the agent the tests at all. So **Level A's seed leg is now
unambiguous: never apply test_patch in the run sandbox.**

**Aider under the standard:** unchanged visibility (compliant) + the §2 integrity fixes +
optional 2-attempt loop. (A hold-out mode is *not* required and would stop being "aider".)

---

## 5. Hardware / topology research (the x86 grading problem)

P2's official grade needs **x86_64 Linux** for native official images. Where can that run?

### 5a. Docker Sandboxes — nested Docker works, platform limits bite
- **Each `docker sandbox` microVM ships its own private Docker daemon** → docker-in-docker
  is a *designed* feature (Docker's own "Why MicroVMs" post). So nested regular containers
  work wherever the sandbox runs.
- **Platform support:** macOS (Hypervisor.framework) + Windows (Windows Hypervisor Platform)
  are **GA**; **Linux is roadmap / not yet shipped** (mid-2026) and requires **Docker
  Desktop**, not plain Engine (which is what most Linux servers/CI run).
- Opened a *third* Level-A topology idea (grade in a nested official container *inside* the
  danno sandbox) — but on ARM64 Mac it hits the same x86 wall below, so it doesn't rescue A.

### 5b. SWE-bench on ARM64 (issue #520 analyzed) — mostly noise for danno
danno uses **SWE-bench Verified = 100% Python.** Issue #520's breakages are almost all in
the **JavaScript** (Chrome `[arch=amd64]`, `pnpm-linux-x64`) and **Java** (`mvnd` no arm64,
Java/Gradle **log-parser** false status, Druid snapshot) paths — SWE-bench *Multimodal/
Multilingual*, which **danno does not use.** Notably the log-parser bug is **Java-only** →
adopting the **Python** log parser under Level B is unaffected.

**What DOES apply to danno's Python instances on arm64:**
- ~**496 of 2,294** full-dataset instances **require x86** (binary conda packages not
  published for ARM): **scikit-learn (192), matplotlib (147), xarray (110)** are the
  blocker families. The other ~78% (django, sympy, requests, flask, sphinx…) build native.
- `make_test_spec` **hardcodes `arch=x86_64`**; the arm64 path exists but "nobody invokes
  it" → must patch or `--namespace ''` to force local native build.
- **Transitive dep-drift:** pip on arm64 pulls *newer* versions than the frozen x86 images
  (Pygments 2.19.2 vs 2.18.0 in sphinx-10323) → **native-arm64 ≠ byte-official**, partly
  undercutting A's "official score" on the Mac.
- Where native arm64 *did* build, all 11 sampled instances matched x86 pass/fail (eval
  *semantics* are sound; the breakage is build/setup + pinning).
- danno's 3 calibration instances: **django-16527 / sympy-20590** build native arm64 clean;
  **astropy-12907** is the risk (C/Cython + pinned numpy; may need from-source build).

### 5c. Parallels x86 Linux on the Mac (route evaluated)
Correction to an earlier assumption: **Parallels Desktop 20.2+ *can* run x86 on Apple
Silicon** (x86 emulation; v19+ Pro has Rosetta-based x86-64 Linux binary translation) —
"really slow," and an x86 VM roughly doubles assigned RAM on the host.
- **Decoupling insight:** the grade leg needs **no Ollama** (grading is diff + tests, no
  model). So "where x86 grading runs" and "where Ollama runs" are independent.
- **Recommended topology:** Ollama **native on macOS** (only place it gets Metal GPU) →
  serves the run leg on the Mac (Docker sandbox, unchanged) → hand `predictions.jsonl` to a
  **Parallels x86 Linux VM** for the grade leg (no Ollama there).
- **Why attractive:** a *full x86 guest presents as a normal x86 box*, so **every #520
  arm64 landmine disappears** (hardcoded arch correct, no dep-drift, scientific-package
  instances work) → **genuinely official-comparable, all-local, no cloud.** Cost = emulation
  speed (fine for batch grading of a *small* select) + RAM + ~120GB disk for images + needs
  Parallels **Pro/Business**.
- **Ollama inside Parallels = NO:** Apple exposes no GPU API to the virtualization
  framework → **CPU-only, ~3–8 tok/s on a 7B** → unusable; and the run leg doesn't belong
  there anyway.

### 5d. What `ollama ps` / `ollama list` do under a Parallels split
- In danno these are **HTTP calls, not CLI shell-outs**: `ollama list` → `GET /api/tags`
  (`commands/ollama.py:22-34`), `ollama ps` → `GET /api/ps` (`commands/ollama.py:42-51`,
  `telemetry/sampler.py:258`); also `/api/show`, warm via `/api/generate`.
- They are issued by the **host-side orchestrator/sampler** against **`localhost:11434`**
  (`bench.py:285-286`), and belong to the **run leg + provenance/sampling only** — the
  sampler wraps the *turn*, **not** `task.grade()`. **Grading issues zero Ollama calls.**
- **Recommended split (orchestrator + run on Mac; only grade in Parallels): nothing
  changes** — those calls run on the Mac vs native Ollama; Parallels never sees them.
- **Whole-danno-in-Parallels:** the calls **degrade gracefully** (`/api/ps` unreachable →
  empty resource rows, no crash; warm refused = non-fatal), but the run leg breaks harder
  anyway (no Docker Desktop sandbox on Parallels' Engine; CPU-only Ollama).

### 5e. x86 Windows 11 PC (route evaluated)
- **Native x86** → SWE-bench official images run at **full speed via Docker Desktop's WSL2
  Linux-container backend**; none of the emulation tax or #520 arm64 issues. *Better* grade
  host than Parallels.
- **"Does the PC need Linux?"** Practically yes, via **WSL2** (bundled with Windows 11, not
  dual-boot): (1) SWE-bench containers are Linux/x86 (native via WSL2 backend); (2) danno is
  POSIX/bash-developed → runs far cleaner *inside* WSL2 than native Windows Python.
- **The simplification:** grade leg needs no Ollama → if the PC is just the **grade box**,
  **don't point danno-on-PC at Mac Ollama at all**. Keep run + Ollama on the Mac
  (unchanged), ship the diff to the PC for native-x86 grading. Zero remote-Ollama plumbing.
- **The user's literal topology (run leg *also* on PC, model on Mac over LAN)** — viable but
  adds: danno in WSL2; `docker sandbox` on Windows (⚠️ verify WSL-integration + nested-virt
  coexistence); **repoint Ollama upstream** `host.docker.internal`→Mac LAN IP (danno
  hardcodes the host at `driver.py:141`/`sandbox.py:50`, only the *port* is env-configurable
  → needs a code lever *or* a host-side socat/ssh port-forward); a **proxy allow-rule** for
  the Mac IP:11434; sampler/provenance `host_url`→Mac (defaults `localhost`, not a CLI
  flag); Mac `OLLAMA_HOST=0.0.0.0` + firewall. Resource telemetry then spans two machines
  (Mac VRAM + PC CPU).
- **The fact that reshuffles it all — does the PC have an NVIDIA GPU?** If **yes**: run
  Ollama natively on the PC (CUDA) → the PC becomes the **single ideal danno host** (native
  x86 grade + local GPU model), Mac drops out. If **no**: borrowing Mac Ollama is the reason
  to keep the Mac, and the run/grade split (Mac=run+model, PC=grade) beats remote-Ollama.
  **← OPEN QUESTION for the user.**

---

## 6. Key conclusions

1. **Split every task into seed/run/grade.** Adopt official code on seed+grade; keep run
   as danno's. This is the through-line.
2. **SWE-bench: diverging on grading was wrong.** Adopt the official specs/parsers (B) or
   the official harness via predictions file (A). The node-id grader bug makes current
   numbers untrustworthy.
3. **Aider: little official *code* to adopt** (self-grading). Replicate the 2-attempt
   protocol + fix the 3 silent integrity bugs (§2).
4. **Standard: honor each benchmark's test-visibility policy.** SWE-bench → hold-out
   (stop applying test_patch pre-run); Aider → stay test-visible. This kills the current
   leaky SWE design and the "self-verify-then-exclude" hybrid.
5. **The grade leg needs no Ollama** — the decoupling that makes both the Parallels-x86 and
   Windows-PC grade boxes clean.
6. **#520's arm64 horrors are mostly JS/Java = wrong datasets for danno.** The real arm64
   tax is narrow: ~20% scientific-package instances are x86-only; the rest build native but
   risk dep-drift → *official-ish*, not official.
7. **For a truly official number:** native x86 (Windows-PC/WSL2 or x86 Linux CI) beats
   emulated Parallels; Parallels-x86 is the all-local option that still avoids every #520
   arm64 landmine, at emulation speed.

---

## 7. Decision points (owner: user)

- **D1 — SWE-bench grading depth:** Level B (in-VM specs+parsers) · Level A (official
  container) · both phased. *Changes host requirements (Docker + images + egress).*
- **D2 — Aider scope:** protocol (2-attempt) + integrity fixes · integrity fixes only ·
  defer aider.
- **D3 — Where the official (P2) grade box lives:** Parallels x86 on the Mac · x86 Windows
  PC (WSL2) · x86 cloud CI · none for now (B-only).
- **D4 — If Windows PC:** does it have an NVIDIA GPU? (yes → single-box PC host; no →
  run/grade split with Mac Ollama). **Blocks the PC-route recommendation.**
- **D5 — Is "official-comparable score" even a goal?** DoR says we never claim an official
  score. If not a goal, Level A's cost buys little and B is the pick.

---

## 8. Needed investigations (verification items)

- **I1 — `swebench` import surface:** can we import `harness.{constants,log_parsers,grading}`
  without pulling `docker`/`datasets`? If not → vendor those modules w/ license headers.
- **I2 — per-repo grade commands:** confirm `MAP_REPO_VERSION_TO_SPECS` covers all repos in
  our `select`; wire django→`./tests/runtests.py`, sympy→its runner, etc. (or emit
  `ungradeable` for non-pytest id shapes rather than silent `False`).
- **I3 — hold-out refactor:** restructure `provision`/`reset` (`swebench.py`) so `test_patch`
  is applied only at grade time; extract the agent's diff cleanly (source-only, exclude
  tests) for the predictions file.
- **I4 — Aider integrity:** implement pristine language-namespaced seeding + dir wipe +
  grade-time test-hash assertion (`aider.py`).
- **I5 — Docker sandbox × WSL2 (Windows):** does `docker sandbox` work through WSL
  integration, and do the sandbox microVM + WSL2 coexist (nested virt)?
- **I6 — remote-Ollama levers:** danno hardcodes `host.docker.internal` upstream
  (`driver.py:141`, `sandbox.py:50`) — only port is env. Decide: add a host lever vs
  host-side port-forward; plus proxy allow-rule for a remote Ollama IP; plus expose
  sampler/provenance `host_url`.
- **I7 — Parallels x86 grade probe:** run the official harness on 1–2 Python instances in a
  Parallels x86 Linux VM; confirm native-to-guest x86 image execution + measure wall-clock.
- **I8 — Windows PC GPU (D4):** the single fact gating the PC-route recommendation.
- **I9 — warmed re-run:** re-run the 9 aider rows under the new pre-warm default to remove
  the model-load confound (independent of the above; cheap).
- **I10 — `--max-turns N` bench flag:** normalize the occ=30 / claurst≈10 / opencode=∞
  fairness gap before any comparative cost claim.

---

## 9. Sources (web-verified 2026-07-09)

- Docker Sandboxes: [Why MicroVMs](https://www.docker.com/blog/why-microvms-the-architecture-behind-docker-sandboxes/) ·
  [docs](https://docs.docker.com/ai/sandboxes/) · [InfoWorld](https://www.infoworld.com/article/4177309/docker-sandboxes-and-microvms-explained.html)
- SWE-bench arm64: [issue #520](https://github.com/SWE-bench/SWE-bench/issues/520) ·
  [Grey Newell — 6× faster native ARM64](https://greynewell.com/blog/swe-bench-arm64-native-containers-6x-faster/) ·
  [ARM vs x86 data (gist)](https://gist.github.com/greynewell/497005bb33641503f1a5874f16578088) ·
  [SWE-bench Verified is Broken](https://greynewell.com/blog/swe-bench-verified-broken-5-things-source-code/)
- Parallels x86 on Apple Silicon: [KB 130217](https://kb.parallels.com/en/130217) ·
  [Rosetta x86-64 Linux](https://docs.parallels.com/parallels-desktop-developers-guide/software-development-specific-functions-of-parallels-desktop/using-rosetta-to-run-x86-64-linux-software-on-apple-silicon-macs)
- Ollama GPU on Apple Silicon VMs: [Chariot — Apple GPUs, Docker & Ollama: pick two](https://chariotsolutions.com/blog/post/apple-silicon-gpus-docker-and-ollama-pick-two/) ·
  [Ollama hardware support](https://docs.ollama.com/gpu)
