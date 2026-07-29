# Printing & Error Reporting — part

Read this part when you write code that **reports progress, logs, prints, or
handles an exception** — which is nearly all of `danno`. It operationalizes the
constitution's **Fail Loud** rule (Working Rule 8): that rule says a failure must
never be silent; this part says *what level it is, where it goes, and through what
mechanism*. The design-of-record behind it is
[`.docs/plan-printing-error-reporting-policy.md`](../../../.docs/plan-printing-error-reporting-policy.md).

The policy is **mandatory**. Its three pillars: a fixed severity taxonomy, a
no-silent-swallow prohibition, and a single standard mechanism with a strict
channel split.

## The five severity levels

Every reported condition is exactly one of these. They map onto the syslog /
stdlib-`logging` model — the mechanism **is** a `logging.getLogger("danno")` in
[`core/log.py`](../../../src/book_em_danno/core/log.py) — with one danno-specific
tier, **TRANSIENT** (level 35, between `WARNING` and `ERROR`); **FATAL** is
`CRITICAL` (50) displayed as `FATAL`.

- **FATAL** — an unrecoverable precondition for the whole command is unmet: an
  invalid `danno.toml`, a missing required binary, or a **security-invariant
  violation** (e.g. a `"**"` / `"*"` / empty sandbox egress allow-list). Log it,
  then abort the command with a non-zero exit. A security-invariant violation is
  ALWAYS FATAL and is never downgraded to a warning or a deferred "hardening".
- **ERROR** — *this* operation definitively failed and the failure must reach a
  **durable record or verdict**, not merely stderr: a persistent auth rejection,
  a malformed upstream response, a crashed request handler, a failed durable
  write. The step/cell is marked failed; the run does not silently continue as if
  it succeeded.
- **TRANSIENT** — an intermittent failure that is *safe to retry*: a network
  timeout, a connection reset, a 429/503, a cold-model load. Logged as a warning
  on first occurrence. **Every TRANSIENT site MUST carry an explicit escalation
  budget** — N attempts, M consecutive failures, or a time window — after which
  it **escalates to ERROR**. A retry with no bound is a swallowed error and is
  forbidden. (Fails once → warn and retry; fails always → error.)
- **WARNING** — a genuine non-fatal anomaly the operator should notice: a usage
  block that is *unparseable* (distinct from absent), a zero-request cell, a
  degraded fallback, a refused overwrite. Logged durably and greppably; the
  command continues.
- **INFO** — normal lifecycle and major/minor step announcements: proxy bound on
  port N, provisioning step 3 of 5, model X selected, converged / no-op.

## No condition is swallowed silently

This is the enforceable core of Fail Loud for code:

- **No** `except …: pass`, and **no** `except …: return None` / `{}` / `False` /
  `0` for an **operational** (non-control-flow) error. If an error and a
  legitimate empty result would return the same sentinel, they MUST be made
  distinguishable and the failure counted. "Absent" and "failed to read/parse"
  are different facts.
- A failure may **never** be observable only in a scrolling stderr or a dropped
  record. A cell or run result MUST reflect any failure that occurred during it.
- **Long-running or threaded code** (the capture proxy, the stub server) MUST
  convert an escaped exception into a **recorded and counted** event — never a
  bare traceback via a framework's default `handle_error`.

## One standard mechanism

- The machinery lives in [`core/log.py`](../../../src/book_em_danno/core/log.py):
  a stdlib `logging.Logger` fanning to a rich `RichHandler` on stderr and, under
  `--log-file`, a plain file handler. Callers pass **plain** text through the
  helpers `log_info` / `log_warn` / `log_transient` / `log_err` / `log_fatal` /
  `log_debug`; each handler's formatter adds the `[LEVEL]` tag (coloured on the
  console, greppable in the file) and the message body is markup-escaped so a
  bracket in a path/IP (`[::1]`) is never eaten by rich's parser. The helpers are
  re-exported from [`core/exec.py`](../../../src/book_em_danno/core/exec.py) so
  existing `from book_em_danno.core.exec import log_warn` imports are unchanged.
  No module invents its own `print` / `Console` / `echo` path for logging.
- The **TRANSIENT *retry* helper** (the retry-with-escalation-budget loop) is
  deliberately **not** built yet — it lands with its first real call site in the
  remediation sweep (no speculative abstraction); `log_transient` records the
  first-occurrence warning today.
- **Emitting a command's data product is the one sanctioned exception** and uses
  the explicit stdout writer `core.exec.console`, never the logger (see the
  channel split).

## Channel separation (split by purpose, not mechanism)

- **stdout = data channel.** A command's parseable product only: `--version`,
  `--help`, the doctor report, the validate results grid, `--format json` JSONL,
  the merged bench markdown. It NEVER carries a log level, and its bytes are
  identical at every verbosity.
- **stderr = live log channel.** Progress, lifecycle, WARNING, ERROR, FATAL —
  shown as it happens. Non-leveled stderr **chrome** (the advise `$ cmd` echo, the
  validator's rich progress UI) shares this channel via `core.log.err_console` but
  carries no `[LEVEL]` tag.
- **file = durable log channel.** A mirror of the **leveled** stream (see below).
  The stderr chrome is *not* file-mirrored — its durable form is the run's own data
  artifacts (the validator's JSON/report, the bench captures), not the log file.

## File logging

The standard mechanism MUST support a file sink. File logging is:

- **Mandatory for long-running / unattended contexts** — `danno bench` sweeps,
  the capture proxy, the stub server — where the stderr stream scrolls past and
  is lost. These write a run log under the run's home
  (e.g. `captures/<run>/danno.log`). This is the direct remedy for the
  invisible-failure class in issues #98 / #99 / #101.
- **Optional (opt-in) for short interactive commands** (`doctor`, `install`, a
  single `sandbox start`), where terminal stderr suffices; a `--log-file PATH`
  flag covers the opt-in.

The file log always captures DEBUG-and-up regardless of console verbosity, so a
failure stays diagnosable without re-running under `-v`. The keystone lands the
`--log-file` capability; auto-opening a run log inside the bench/proxy run-homes
is tracked as follow-on in the DoR.

## Verbosity

One knob moves the **console (stderr) threshold only**; the data channel is
unaffected.

- `-q` / `--quiet` — FATAL, ERROR, WARNING.
- default — FATAL, ERROR, WARNING, INFO (normal narration).
- `-v` / `--verbose` — the above plus DEBUG and live child transcripts.

TRANSIENT first-occurrences show at default (they are warning-shaped); an
escalation to ERROR always shows.

## Enforcement

- Ruff **flake8-print (`T20`)** bans bare `print` in `src/`; the few legitimate
  data/sandbox prints carry `# noqa: T201` with a reason. (Enabling `T20` is landed
  with the bare-print cleanup PR, not the keystone — turning it on before those
  `# noqa`s exist would break the gate; tracked in the DoR.)
- In review, an `except` for an operational error must show where the failure is
  recorded/counted, and a retry path must name its escalation budget.

## See also

- [`../constitution.md`](../constitution.md) — **Fail Loud** (Working Rule 8) and
  the Engineering-Discipline pointer this part expands.
- [`python.md`](python.md) — the `core/exec.py` logging helpers + Runner, and the
  two-tier advise/`--apply` policy.
- [`.docs/plan-printing-error-reporting-policy.md`](../../../.docs/plan-printing-error-reporting-policy.md)
  — the full design-of-record: current-state research, the call-site remediation
  backlog, the channel-tagging migration table, and rollout sequencing.
