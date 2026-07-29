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
stdlib-`logging` model (so a future move to `logging.getLogger("danno")` is a
drop-in) with one danno-specific tier, **TRANSIENT**.

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

- Use the shared reporting helpers in [`core/exec.py`](../../../src/book_em_danno/core/exec.py)
  (`log_info` / `log_warn` / `log_err`, plus `log_debug`; a `FATAL` and a
  `TRANSIENT` path are part of the taxonomy above). No module invents its own
  `print` / `Console` / `echo` path for logging.
- The mechanism is the drop-in for stdlib `logging` + rich's `RichHandler`; the
  level names above are chosen to match so the migration stays mechanical.
- **Emitting a command's data product is the one sanctioned exception** and uses
  an explicit stdout writer, never the logger (see the channel split).

## Channel separation (split by purpose, not mechanism)

- **stdout = data channel.** A command's parseable product only: `--version`,
  `--help`, the doctor report, the validate results grid, `--format json` JSONL,
  the merged bench markdown. It NEVER carries a log level, and its bytes are
  identical at every verbosity.
- **stderr = live log channel.** Progress, lifecycle, WARNING, ERROR, FATAL —
  shown as it happens.
- **file = durable log channel.** A mirror of the stderr stream (see below).

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
failure stays diagnosable without re-running under `-v`.

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
  data/sandbox prints carry `# noqa: T201` with a reason.
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
