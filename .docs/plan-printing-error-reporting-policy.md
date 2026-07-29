# Plan — Printing & Error-Reporting Policy (issue #102)

Design-of-record for a project-wide **printing, logging, and exception-handling**
policy for `danno`. Operationalizes the Constitution's **Fail Loud** rule so that
"when something goes wrong, where does it show up, and who notices?" has one
answer instead of a different one in every module.

- **Governing issue:** #102 (standardize progress reporting, logging & exception
  handling). Same "invisible failure" theme as #98 (guaranteed-empty capture
  sidecars), #99 (o4-mini 401s silently accepted), #101 (capture egress log).
- **Authoritative rule home:** the new Constitution part
  [`parts/printing-and-error-reporting.md`](../.specify/memory/parts/printing-and-error-reporting.md)
  (thin pointer in `constitution.md` → Engineering Discipline).
- **This file** is the investigation + remediation backlog behind that policy.

---

## 1. Current state (as-built research)

### 1.1 What logging model we have

danno does **not** use Python's stdlib `logging`. It has a hand-rolled leveled
printer in `core/exec.py` over a single global `rich.Console()`:

```
core/exec.py:33  console = Console()          # the ONLY Console in the tree
         :43  log_info(msg)   → [INFO]
         :47  log_warn(msg)   → [yellow][WARN][/yellow]
         :51  log_err(msg)    → [red][ERROR][/red]
         :55  log_debug(msg, *, verbose)  → printed only if verbose
```

The level set **DEBUG / INFO / WARN / ERROR** is exactly the syslog (RFC 5424)
severity model that stdlib `logging` adopts (`DEBUG=10, INFO=20, WARNING=30,
ERROR=40, CRITICAL=50`). So we are conceptually closest to **stdlib `logging`'s
level model**, but implemented ad hoc and missing the machinery:

| stdlib `logging` gives | danno has |
| --- | --- |
| Level constants incl. `CRITICAL`/`FATAL` | DEBUG/INFO/WARN/ERROR only — **no FATAL tier** (fatals are just `log_err` + exit) |
| Runtime level filtering | a manual `if verbose:` bool on DEBUG only |
| Logger hierarchy / per-module loggers | one global `console` |
| Handlers (console + file + syslog) | one stdout sink |
| Formatters (timestamp, module, PID) | bare `[LEVEL] msg` string tags |
| Records other tools can consume | none |

The validator layer has a **better** pattern — `danno_validator/events.py`
(`ValidateEvent`) + a `Reporter`/`ConsoleReporter` split — that decouples emit
from render, but it is scoped to validate/bench progress, not general logging.

**Idiomatic upgrade:** rich ships `RichHandler` *for* stdlib `logging`. Moving to
`logging.getLogger("danno")` + `RichHandler` (console) + `FileHandler` keeps the
current look while getting levels, filtering, the stdout/stderr split, and a file
sink for free.

### 1.2 Documented requirements today

No single spec exists — that gap is what #102 closes. What exists is scattered:

- **Constitution:** Working Rule 8 **Fail Loud** + Configuration-is-Code — the
  governing principles; neither names levels or destinations.
- `parts/python.md:22` — names it only: "`core/exec.py` — logging helpers + the
  Runner."
- `.docs/ux-danno-validate-cli.md` — documents `-v/--verbose` = stream harness
  transcripts live (`Runner(verbose=True)`).
- `.docs/plan-claurst-swe-benchmarks.md:116` — implicit requirement: "`--verbose`
  dumps ANSI DEBUG logs to stdout → **never use it for parsing**."
- `.docs/plan-danno-validator.md:189` — "`--format json` is JSONL, one JSON event
  per line on **stdout**" → stdout is a data channel; logs must not corrupt it.

Two de-facto requirements are therefore already documented: **verbose/DEBUG must
not pollute parseable output**, and **stdout carries JSONL data**.

### 1.3 Complete output surface (exhaustive sweep of `src/`)

| Mechanism | Count | Where | Sink |
| --- | --- | --- | --- |
| Shared `rich.Console()` (the only one, `exec.py:33`) | 39 `console.print` + the 4 `log_*` are among them | see §4 | **stdout** |
| Bare `print(..., file=sys.stderr)` | 1 | `telemetry/report.py:635` | stderr |
| Bare `print(...)` (markdown product) | 1 | `telemetry/report.py:632` | stdout |
| Bare `print(...)` inside a generated sandbox test string | 2 | `level2.py:123,125` | not host output |
| Typer/Click framework (`--help`, usage errors) | — | framework | stdout / stderr |
| Uncontrolled: HTTP handler tracebacks via stdlib default `handle_error` | — | `capture/proxy.py`, `stubai/server.py` | stderr |

**Confirmed absent** (all grepped, all NONE): `typer.echo`/`click.echo`/
`rich.print`, rich auto-widgets (`Progress`/`Live`/`Status`/`track`/`Table`/
`Panel`/`print_json`), interactive prompts (`input`/`typer.prompt`/`confirm`/
`Prompt`/`Confirm`/`getpass`), `sys.stdout/stderr.write`, `os.write`,
`traceback.print_*`, `warnings.warn`, `pprint`.

**Key facts for the policy:**

1. Everything funnels through **one stdout Console**, so WARN and ERROR currently
   land on **stdout**, mixed with machine-parseable data — the exact hazard the
   docs warn about.
2. There is **no operational log file**. danno writes structured *data* artifacts
   (capture JSONL, `egress.json`, provenance/report/menu/results JSON) but no
   run log of the INFO/WARN/ERROR stream.
3. The two long-running HTTP servers suppress their own `log_message` and have no
   `handle_error` override, so a handler crash escapes to **stderr only** — the
   one uncontrolled raw-traceback path.

---

## 2. The policy (summary; authoritative text in the part)

### 2.1 Severity taxonomy — five levels

| Level | Meaning | Destination | Aborts? |
| --- | --- | --- | --- |
| **FATAL** | Unrecoverable; a whole-command precondition is unmet (bad config, missing binary, **security-invariant violation** like `"**"` egress). | log + non-zero exit / raise | Yes — whole command |
| **ERROR** | *This* operation definitively failed; must reach a durable record/verdict, never only stderr (persistent auth failure, malformed response, crashed handler). | recorded event + log | The step/cell is marked failed |
| **TRANSIENT** | An intermittent failure that is safe to retry (network timeout, connection reset, 429/503, cold model load). Logged as a warning on first occurrence; **escalates to ERROR once it exceeds a retry/threshold budget** (fails every time). | log + counter; ERROR on escalation | Only after budget |
| **WARNING** | A genuine non-fatal anomaly the operator should notice (usage *unparseable* vs absent, 0-request cell, degraded fallback, refused overwrite). | log (durable, greppable) | No |
| **INFO** | Normal lifecycle / major & minor step announcements (proxy bound on port N, provisioning step 3/5, model X, converged/no-op). | log | No |

**TRANSIENT is the new tier.** The defining property is the **escalation rule**:
a TRANSIENT with no bound is a swallowed error. Every TRANSIENT site MUST carry an
explicit budget (N attempts, or M consecutive, or a time window) after which it
becomes an ERROR that reaches the verdict. Maps to a custom `logging` level 35
(between `WARNING` 30 and `ERROR` 40).

### 2.2 No silent swallowing (the core prohibition)

- No `except …: pass` and no `except …: return None`/`{}`/`False`/`0` for an
  **operational** (non-control-flow) error. The single most repeated bug in the
  audit is a caught parse/IO error returned as the same sentinel a *legitimate*
  empty would return — "absent" and "failed to parse/read" MUST be distinguishable,
  and the failure counted.
- Long-running or threaded code (the capture proxy, the stub server) MUST convert
  an escaped exception into a **recorded + counted** event — never a bare stderr
  traceback via stdlib's default `handle_error`.
- No failure may be observable **only** in stderr or a dropped record. A cell/run
  result must reflect any failure that occurred during it (the through-line of
  #98, #99, #101).

### 2.3 Standard mechanism

One reporting API for the whole codebase — the `core/exec.py` helpers today, a
`logging.getLogger("danno")` facade after the migration. No module invents its
own `print`/Console/echo path. Data output (a command's product) is the one
sanctioned exception and uses an explicit stdout writer, never the logger.

### 2.4 Channel separation

- **stdout = data channel.** A command's product: `--version`, `--help`, doctor's
  report, the validate results grid, `--format json` JSONL, the merged bench
  markdown. Must stay parseable; never carries a log level.
- **stderr = live log channel.** Progress, lifecycle, WARNING, ERROR, FATAL,
  shown as it happens.
- **file = durable log channel.** A mirror of the stderr stream (see §3).

Split by **purpose**, not mechanism: within the one shared Console, `log_warn`/
`log_err` (operational) route to stderr; doctor's grid and `--version` (product)
stay on stdout. Click already routes `--help`→stdout/exit0 and usage errors→
stderr/exit2 correctly; don't touch the framework paths.

### 2.5 Verbosity model (the adjustment #102 calls for)

Today verbosity is a single `--verbose/-v` bool that only un-gates DEBUG, and
INFO/WARN/ERROR all print unconditionally to stdout. The target model:

| Mode | Flag | Console (stderr) shows | Notes |
| --- | --- | --- | --- |
| quiet | `-q/--quiet` | FATAL, ERROR, WARNING | for scripts that want only anomalies |
| default | (none) | FATAL, ERROR, WARNING, INFO | the normal run narration |
| verbose | `-v/--verbose` | + DEBUG, + live child transcripts | today's `-v` behavior, plus DEBUG on the console |

- The **data channel (stdout) is unaffected by verbosity** — `--format json` and
  `--version` produce identical bytes at every level. Only the log channel's
  console threshold moves.
- The **file log always captures DEBUG-and-up** regardless of console verbosity,
  so a failure is diagnosable after the fact without re-running under `-v`.
- `TRANSIENT` first-occurrences show at default (they are WARNING-shaped);
  their escalation to ERROR always shows.

### 2.6 Enforcement

- Enable ruff **flake8-print (`T20`)** on `src/` to ban bare `print`; the two
  legitimate data/sandbox prints carry `# noqa: T201` with a reason. New bare
  prints then fail the gate.
- Review rule: a PR that adds an `except` for an operational error must show where
  the failure is recorded/counted, and any retry path must name its escalation
  budget.
- **Deferred rationale:** the `T20` rule is **not** enabled in the
  policy-authoring commit, because turning it on before the code migration would
  not itself go red (it only bans bare `print`, and there is essentially one to
  annotate) — but the *stdout→stderr* routing and the logger facade are the real
  companions, and they are the remediation phase below. This commit is docs-only;
  it states the rule and books the enforcement as tracked follow-up (surfaced
  here rather than silently omitted).

---

## 3. Does file logging fall out of this policy?

**Partly — it is a required capability, and mandatory in some contexts, optional
in others.**

- The standard mechanism **MUST support a file sink** (a `FileHandler`, or a
  second `rich.Console(file=…)`), because §2.2's "durable, greppable" and "no
  failure observable only in stderr" cannot be met by a scrolling terminal alone.
- File logging is **MANDATORY for long-running / unattended contexts** — `danno
  bench` sweeps, the capture proxy, the stub server — where stderr scrolls past
  and is lost across a long run (this is literally the #98/#99/#101 failure mode).
  These write a run log to the existing per-run home (`captures/<run>/danno.log`).
- File logging is **OPTIONAL (opt-in) for short interactive commands** (`doctor`,
  `install`, a single `sandbox start`), where stderr on the terminal — or the
  user redirecting it — is sufficient. A `--log-file PATH` flag covers the opt-in.

So: the policy does not make *every* invocation write a file, but it does require
the mechanism to offer one and require it wherever the log would otherwise be
lost. That is the natural consequence of "durable + greppable" + "no invisible
failure," not a separate feature.

---

## 4. Channel-tagging migration table (the 39 `console.print` + 4 `log_*`)

Target channels: **stdout** (data), **stderr + file** (live log + durable
mirror), **file (+stderr under -v)** for DEBUG. Rule: narration of what danno is
doing → log; the command's product → data.

### A. Shared helpers — `core/exec.py` (every `log_*` caller inherits these)

| line | emits | → channel |
| --- | --- | --- |
| 44 `log_info` | `[INFO]` step/lifecycle | stderr + file |
| 48 `log_warn` | `[WARN]` | stderr + file (**currently stdout — the move**) |
| 52 `log_err` | `[ERROR]` | stderr + file (**currently stdout — the move**) |
| 57 `log_debug` | `[DEBUG]` verbose-gated | file always; stderr only under `-v` |
| 238, 266 | `  $ <cmd>` advise/run echo | stderr + file (narration; stderr still shows on terminal, so advise copy-paste UX is unaffected) |

A new `log_fatal` (or `log_err(..., fatal=True)`) is added for the FATAL tier;
today fatals are `log_err` + `typer.Exit`/raise with no distinct level.

### B. `cli.py` · `commands/install.py`

| line | emits | → channel |
| --- | --- | --- |
| cli.py:54 | `danno <ver>` (`--version`) | stdout (data) |
| install.py:40 | managed-file diff/content (DIFF mode) | stdout (data) |

### C. `commands/doctor.py` (9) — the whole report is the product → **stdout**

`31` PASS · `35` FAIL · `38` WARN · `39` `fix:` · `61,62` headers · `135` blank ·
`137` failed-summary · `141` passed-summary. (doctor is a report command like
`git status`; report → stdout, exit code = machine signal. Its FAIL/WARN are
result rows, not log events.)

### D. `danno_validator/console.py` (22) — progress = log, results = data

| lines | emits | → channel |
| --- | --- | --- |
| 32, 51, 52, 54, 61 | plan preamble (title, config rows, sandboxes, dry-run hint) | stderr + file |
| 55 | ⚠ slow/paid warning | stderr + file |
| 64 | `▶` phase beat | stderr + file |
| 68, 70, 74, 76 | per-tier live status (config-start / running / done / skip) | stderr + file |
| 111 | menu "↳ uncomment…" instructional hint | stderr + file |
| 119 | `✗ --strict` failure notice | stderr + file |
| 79, 80, 90 | `── results ──` grid (header + rows) | stdout (data) |
| 96, 101, 105 | swept counts / taxonomy / baseline verdict | stdout (data) |
| 108, 110, 117 | report / menu / results-json artifact paths | stdout (data) |

(13 log + 9 data = 22. The `ValidateEvent`→`ConsoleReporter` split means the
channel rule lands in this one class.)

### E. Bare prints — `telemetry/report.py` · `level2.py`

| line | emits | → channel | action |
| --- | --- | --- | --- |
| report.py:632 | merged markdown report | stdout | keep bare (Unix-filter product); `# noqa: T201` |
| report.py:635 | `wrote <path>` diagnostic | stderr + file | **convert to `log_info`** (after stderr routing lands) |
| level2.py:123,125 | generated fizzbuzz test output | N/A — sandbox-side | **no change** — these are inside a triple-quoted test-source string, so ruff's AST-based T20 never flags them (a `# noqa` there would corrupt the generated VM test); the §1.3 grep matched string content, T20 does not |

As-built (§4E PR): enabling `T20` repo-wide (`ninja check` runs `ruff check .`)
also surfaced `scripts/portability/probe.py` — a std-lib-only operator diagnostic
whose `print`s ARE its stdout report and which deliberately cannot import danno's
logger. T20's target is the `src/` package, so that script carries a scoped
`per-file-ignores` entry, not per-line `# noqa`s.

---

## 5. Remediation backlog (call-site inventory)

Follow-up work tracked against the policy. Type per the §2.1 taxonomy. `✗` =
MISSING (should check, doesn't); `⚠` = present but mis-leveled/narrow; `✓` =
already correct (reference).

### capture/proxy.py — #102 Part A seed

- ✓ (DONE step 5) `log_message` stays silent for per-request *access* noise, but
  `capture_proxy` now logs lifecycle at **INFO** (bound port → upstream on start;
  served-call count on close) — the proxy-start/bound-port gap.
- ✓ (DONE step 5) `handle_error` override on `_CaptureServer` → synthetic `error`
  record (persist only) + `handler_errors` counter + **ERROR** log, instead of a
  bare stderr traceback with a dangling request record.
- ✓ (DONE step 5) `URLError`→502 now also logs at **TRANSIENT** (retry-safe from
  the harness's side) before synthesising the 502.
- ✓ bind `OSError`→`CommandFailedError` (263) — **FATAL**.

### capture/usage.py

- ⚠ `_sse_chunk_usage` / `_ndjson_line_usage` `JSONDecodeError→None` (129–143):
  absent == unparseable; distinguish + count the unparseable — **WARNING**.

### capture/egress.py — biggest silent-failure cluster

> **Deferred to when #101 merges.** `capture/egress.py` was introduced by #101 (the
> sbx egress-reachability log) on a sibling branch; it is NOT in this keystone-based
> `log-swallow-sweep` tree, so its call sites can't be remediated here. Fold this
> cluster in once #101 and the sweep converge. Likewise `commands/sandbox.py`
> `record_egress` (#101) below.

- ⚠ `except OSError: return None` (58–61): probe never ran — **TRANSIENT→ERROR**.
- ✗ `returncode != 0 → return None`, `stderr` discarded (62–63): the audit
  definitively failed — **ERROR**.
- ⚠ `json.loads` except / non-dict → None (64–68): malformed sbx output —
  **WARNING**.
- ⚠ `warn_on_blocked` non-list → `[]` (76): could hide real egress blocks —
  **WARNING**.
- ✗ `mkdir` / `write_text` (105–107): raw traceback on the durable audit write —
  **ERROR**.

### core/exec.py

- ✓ (DONE step 7) `capture()` `subprocess.run` & `_capture_watched` `Popen`: a missing
  binary (`FileNotFoundError`) now translates to `CommandNotFoundError` on BOTH paths
  (parity), not a raw `OSError` the caller can't tell from a non-zero exit — **ERROR**.
- ⚠ win32 `taskkill check=False` (148), `killpg` suppress (158), `on_kill` reaper
  `suppress(Exception)` (400), reader re-raise only if `breach is None` (416):
  post-kill degradations go silent — **WARNING**.

### core/registry.py

- ⚠ `load()` `except (JSONDecodeError, OSError): return {}` (26–30): corrupt
  registry == empty, defeats the name-collision guard — **WARNING**.
- ✓ (DONE step 7) `record()` `mkdir`+`write_text`: a failed durable write now raises a
  typed `RegistryError` with the path/cause (the name-collision guard going blind is
  definitive), not a raw traceback — **ERROR**. (Concurrent-clobber locking still deferred.)

### commands/sandbox.py + sandbox_cli.py

- ✗ `configure_proxy` allow-list (431–444) **and** `policy_allow_argv`
  (sandbox_cli.py:167–192): no check rejects `"**"`/`"*"`/empty egress before
  opening — the security invariant — **FATAL**.
- ✓ (DONE step 7) `_capture_session`: `generate(apply=True)` is now INSIDE the try/finally
  that restores `opencode.jsonc`, so any mid-setup raise (generate or `captures_running`
  `__enter__`) restores the user's committed config byte-for-byte — **ERROR**.
- ✓ (DONE step 7) `_build_env_file`/`_provided_env` `Path(f).read_text()`: a missing/unreadable
  `--env-file` now raises `CommandFailedError` naming the file, not a raw `FileNotFoundError` —
  **ERROR**.
- ⚠ `live_sandbox_names` (141) & `_sbx_policy_initialized` (158) ignore
  returncode → tool error masquerades as empty/uninitialized — **WARNING**.
- ⚠ `seed_onboarding` corrupt `.claude.json`→`{}` then overwrite (331): silent
  clobber — **WARNING**; provision missing-`opencode.jsonc` warns via `log_info`
  w/ hand-rolled `[yellow]WARN[/yellow]` (486): mis-leveled — **WARNING**.
- ⚠ `record_egress` unguarded in `finally` (1246–1260): its raise masks the real
  exception — **WARNING**.

### commands/ollama.py — probes conflate transient with definitive

- ⚠ `verify_responds` (236) & `tool_call_probe` (257) → `False` on
  URLError/OSError/JSONDecodeError: a blip == a real capability-failure verdict —
  **TRANSIENT→ERROR**.
- ⚠ `ensure_model` pull, no retry (189): registry blip fails permanently on first
  try — **TRANSIENT→ERROR**.
- ⚠ `installed_tags` swallows `JSONDecodeError`→`set()` (50): malformed == no
  models — **WARNING**.
- ✓ `warm_model` URLError→`log_warn` "non-fatal" (195): the fail-soft-with-log
  model to copy.

### commands/install.py · tools.py

- ⚠ install `ensure_model` loop aborts on first pull failure (102): no retry —
  **TRANSIENT→ERROR**; `present = installed_tags()` blind if Ollama unreachable
  (101), unlogged — **WARNING**.
- ✓ (DONE step 7) tools `filecmp.cmp`/`shutil.copy2` & provenance `write_text`: an `OSError`
  now raises `ToolInstallError` (which install.py's except set catches → the tool is reported
  failed), not a raw traceback that escapes it — **ERROR**.
- ⚠ `install_generic_git` clones but never runs installer under `--apply` (122):
  reports no failure → "ready" overstated — **WARNING**.

### config/loader.py · schema.py · generate.py

- ✓ (DONE step 7) `loader.py` `read_text` after `is_file`: `OSError`/`UnicodeDecodeError`
  now wrapped into the `DannoConfigError` contract — **FATAL**.
- ✓ (DONE step 7) `generate.py` `json.loads` of the user's claurst `settings.json`:
  an unparseable file raises `ValueError` (we refuse to overwrite it) — **FATAL**; a
  valid-but-non-object payload now `log_warn`s the data-loss instead of silently
  discarding it — **WARNING**/data-loss.
- ✓ (DONE step 7) `generate.py` `scan_agent_frontmatter` agent `.md` `read_text` now
  `log_warn`s an unreadable def and records it present-but-keyless — **WARNING**.
- ⚠ `schema.py` `default_agent` (default `"pm"`) never validated to exist —
  **WARNING**.

### commands/doctor.py

- ⚠ `_safe` `except Exception → False` (155): swallows the cause even under `-v` —
  **WARNING** (`log_debug` the exception).
- ✗ no danno.toml load/validate preflight check: malformed config passes doctor
  clean, explodes later — **WARNING**.

### stubai/server.py · script.py (test harness — same shape as the proxy)

- ✓ (DONE step 5) `handle_error` override on `StubServer` → synthetic `error`
  transcript record + `handler_errors` counter + **ERROR** log (mirrors
  `_CaptureServer`); `log_message` stays silent for access noise only.
- ✓ (DONE step 5) streaming `wfile.write`/`flush` loop now catches
  `BrokenPipeError`/`ConnectionResetError` and logs **TRANSIENT** (the stub exists
  to kill clients mid-stream — the response record is already written).
- ✓ (DONE step 5) `int(Content-Length)` (99): a non-integer header is logged at
  **ERROR** and the body treated as empty (no handler crash).
- ✓ (DONE step 7) setup `mkdir`/`write_text("")`: a bare `OSError` preparing the
  transcript now raises `CommandFailedError` — **FATAL**.
- ✓ port bind `OSError→CommandFailedError` (233) — **FATAL** (the model to copy).

---

## 6. Rollout sequencing

**As-built status (2026-07-29):** step 1 shipped as docs (commit `8c09068`).
Steps 2–3 shipped as the **keystone** (new `core/log.py` on stdlib `logging` +
`RichHandler`; `log_fatal`/`log_transient`; `TRANSIENT`=35; `--verbose`/`--quiet`/
`--log-file` wired; the FATAL egress guard at `policy_allow_argv`; and the
`core/exec.py` `log_warn`/`log_err`/advise-echo reroute to stderr) **minus** the
validator ConsoleReporter split (step 2's "~13 progress lines" — DoR §4D) and the
bare-print cleanup + `T20` (§4E / step 6), which land as **two follow-on PRs**
stacked on the keystone (splitting keeps each diff reviewable). Steps 4 (auto
run-log wiring), 5 (handler-error capture), and 7 (the §5 sweep) remain.

1. ✅ **Policy:** the part + the Constitution pointer + this DoR. Docs-only.
2. ✅ **Logger facade + channels (keystone):** at the `exec.py` chokepoint the
   `log_*` helpers now delegate to `core/log.py` — a `Console(stderr=True)`
   RichHandler + an optional file sink; `log_fatal` added; the `TRANSIENT` level
   (35) introduced. `log_warn`/`log_err`/advise echoes route to stderr; data-channel
   prints stay on stdout. **Follow-on PR (§4D):** move the ~13 ConsoleReporter
   progress lines to the log channel. **Then** convert `telemetry/report.py:635`
   (in the §4E follow-on PR).
3. ✅ **Verbosity (keystone):** `-q/--quiet` + `-v/--verbose` gate the console
   threshold via `configure_logging`; the file log always captures DEBUG-and-up.
4. ✅ **File sink for long-running contexts:** `core.log.run_log(path)` — a
   context manager that attaches a DEBUG-and-up `[LEVEL] msg` file mirror ON TOP of
   the console handler (console verbosity untouched) and detaches + closes it on
   block exit, so consecutive sweeps in one process each get their own log. Wired
   into the `validate` sweep (`<out_dir>/danno.log`, `run.py`) and the `bench`
   sweep (`<out_dir>/danno.log`, `suites/bench.py`), scoped to the real-run body
   (a `--dry-run` opens no log).
5. ✅ **Handler-error capture:** `handle_error` overrides on `_CaptureServer`
   (`capture/proxy.py`) and `StubServer` (`stubai/server.py`) → a synthetic `error`
   record + a `handler_errors` counter + an ERROR log, never a bare stderr
   traceback. Folded in the proxy/stub-adjacent §5 items: capture-proxy lifecycle
   INFO logging + `URLError`→TRANSIENT; stub streaming `BrokenPipeError`→TRANSIENT
   + non-integer `Content-Length`→ERROR-and-empty-body.
6. **Enforcement (§4E follow-on PR):** enable ruff `T20`; annotate the legitimate
   bare prints (`report.py:632`, `level2.py:123,125`) + convert `report.py:635`.
   Deferred out of the keystone because turning `T20` on before those `# noqa`s
   exist would break the gate.
7. **Remediate §5** call sites against the taxonomy, most-severe first (FATAL
   security-invariant checks → ERROR durable-record gaps → TRANSIENT retry
   budgets → WARNING anomalies). Progress on branch `log-swallow-sweep`:
   - **FATAL tier landed:** `loader.py` read guard, `generate.py` claurst-settings +
     agent-def guards, `stubai/server.py` transcript-prepare guard.
   - **ERROR tier landed:** `core/exec.py` `capture()`/`_capture_watched`
     missing-binary → `CommandNotFoundError`; `core/registry.py` `record()` →
     `RegistryError`; `commands/sandbox.py` `_capture_session` rewrite moved inside
     the restore try/finally + `_build_env_file`/`_provided_env` → `CommandFailedError`;
     `commands/tools.py` copy/provenance → `ToolInstallError`. (`capture/egress.py`
     ERROR sites deferred to #101 — file not in this tree.)
   - TRANSIENT and WARNING tiers follow.

## Related

- Constitution: **Fail Loud** (Working Rule 8), Configuration-is-Code.
- Part: [`parts/printing-and-error-reporting.md`](../.specify/memory/parts/printing-and-error-reporting.md).
- Issues #98, #99, #101; memories `danno-benchmarks-the-triple`,
  `sandbox-security-contract-fail-loud`.
