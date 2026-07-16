# Plan — `--no-save-captures` truly writes nothing to disk

**Status:** DESIGN (2026-07-15, not yet implemented)
**Companions:** [`plan-bench-runaway-gates.md`](plan-bench-runaway-gates.md) (the always-on
capture proxy = gate sensor, PR #88), [`plan-runaway-gates-validation.md`](plan-runaway-gates-validation.md)
(F4 — the temp-dir *leak* fix this plan supersedes). Constitution: Working Rule 8
(fail-loud), "Simplicity first", "Configuration is Code".

## 1. The contract, sharpened

`--no-save-captures`' promise is **"persist nothing."** Today danno interprets that as
*"write the full wire captures to a temp dir, use them, then delete them"*
([F4 fix](plan-runaway-gates-validation.md#f4)). That is a band-aid over the wrong wound:
the sensitive data — the model's **prompts and completions** — still lands on disk for the
duration of the run, accumulates across aborts (the F4 bug), and is only *scheduled* for
deletion. A user who passes `--no-save-captures` is asking that those bytes **never touch
disk at all**. F4 made the deletion reliable; this plan makes the write never happen.

The insight that makes this cheap: **nothing danno needs from capture under
`--no-save-captures` requires the message bodies on disk.** The runaway monitor and the
report both run on *derived numbers*, which the proxy already computes in RAM.

## 2. Audit — every disk write in the bench capture path

| Writer | Location | Contains prompts/completions? | What it feeds |
|---|---|---|---|
| Proxy JSONL (`_record`) | `capture/proxy.py:106` | **YES** — full request + response `body` | post-hoc metrics + transcript (re-read from disk) |
| Readable transcript (`write_transcript`) | `suites/base.py:255` → `wire_metrics.py:313` | **YES** — renders every prompt/tool-call/completion | human-readable capture artifact |
| Per-cell metrics JSON (`write_metrics`) | `suites/base.py:253` | no — `TurnWireMetrics` is token/latency/context **numbers** | the report |
| `bench.json` / `results.json` | `suites/bench.py` | no (verdicts + harness `error_summary`; not model wire) | the report |
| `provenance.json` | `telemetry/provenance.py` | no (config + host facts + gate caps) | the report |

Under `--no-save-captures` today, the first **two** rows (bodies) are written to a
`danno-bench-cap-*` mkdtemp root and then `rmtree`d. Those are the writes to eliminate.

What is fed *without* disk at all today:

- **The `GateTally` (Gate 1/2 sensor).** `capture/proxy.py` calls `tally.record(...)`
  **in-process** as each response streams through — the watchdog reads it from memory. The
  JSONL file is not on that path. **Runaway monitoring already needs zero disk.**
- **Every number the report shows.** `wire_metrics.parse_capture_records` reads each
  record's `body` **only** to call `extract_usage(body)` — i.e. it wants the `usage`
  block (token counts) and the request/response timestamps, never the message text. Token
  split, cached tokens, RTT, tok/s, and the context-occupancy curve all fall out of
  `(path, method, ts_request, ts_response, usage)`.

Only the **readable transcript** genuinely needs the bodies — and a transcript is a
*saved* artifact by definition. Under "persist nothing" there is simply no transcript.

## 3. Design — the proxy accumulates body-free summaries; disk is an opt-in side effect

Separate the two things the proxy conflates today ("record everything to a file" vs. "keep
a live sensor"):

1. **Always, in RAM:** as each call completes, the proxy computes a **`CallSummary`** — a
   pure-number carrier `(seq, method, path, ts_request, ts_response, usage | None)` — and
   (a) feeds the `GateTally` [unchanged, F1 path-based], (b) appends the summary to an
   in-memory list on the proxy server. **No message text is retained.** This is cheap (a
   few ints per call) and is the single source the report's wire metrics roll up from.
2. **Only when persisting:** if `save_captures` is true, the proxy *also* writes the full
   request/response JSONL to disk (today's `_record`), exactly as now.

So the body-free numbers are always available in memory; the body-bearing disk write is a
gated side effect. Consequences:

- **`--no-save-captures`:** `persist=False`. No mkdtemp, no `capture_dir`, no JSONL, no
  transcript — **zero capture bytes on disk.** The report still gets full token/latency/
  context metrics from the in-memory summaries. The runaway gates work unchanged.
- **`--save-captures` (default):** `persist=True`. Files written as today; metrics roll up
  from the same in-memory summaries (uniform), and the transcript is written from the
  on-disk JSONL as today.

### 3.1 Layering (the one non-obvious constraint)

`RequestMetric`/`TurnWireMetrics` live in `danno_validator.telemetry.wire_metrics`, but the
proxy is in `book_em_danno.capture` — which must not import the validator (the acyclic
`core/capture → validator` layering, same reason `GateTally` is structural). So define
`CallSummary` at the **capture layer** (`capture/usage.py` or a new `capture/summary.py`:
pure stdlib, just the normalized `usage` dict + path/method/timestamps), and have
`wire_metrics` gain `metrics_from_summaries(list[CallSummary]) -> TurnWireMetrics` that maps
each `CallSummary` to a `RequestMetric`. `parse_capture_records` (the on-disk path) stays
for the save-mode / re-read case; both converge on `rollup`.

### 3.2 The honest RAM boundary

To *forward* a request the proxy must hold its bytes in memory transiently (it already
buffers the whole response before replay). That RAM residency is inherent to being a proxy,
not a capture: the bytes live only inside the request handler's stack frame and are freed
when it returns — **never written to disk, never appended to any run-scoped structure.**
The contract this plan meets is precise: *"`--no-save-captures` writes no capture data to
disk and retains no message bodies beyond the forwarding of a single request."* (A future
hardening could stream-re-originate instead of buffering, shrinking even the RAM window;
out of scope here — the contract is about persistence.)

## 4. Touchpoints

- `capture/summary.py` (new) — `CallSummary` dataclass (numbers + routing only).
- `capture/proxy.py` — `CaptureProxyConfig` gains `persist: bool` (default preserving
  today's behavior); `_CaptureServer` holds a `list[CallSummary]` + lock; `_Handler._proxy`
  builds a `CallSummary` every call (feeds tally + appends), and calls `_record` (bodies →
  disk) **only when `persist`**. Expose the summaries via `read_summaries()`-style accessor.
- `wire_metrics.py` — `metrics_from_summaries(summaries)`; `CallSummary → RequestMetric`.
- `suites/bench.py` `_setup_bench_capture` — under `not save_captures`: **no `mkdtemp`, no
  `capture_dir`**; build the binding in metrics-only mode (`persist=False`). Delete the F4
  temp-root computation + the `finally rmtree` (nothing to clean). Keep the F4
  `--no-save-captures --capture-dir` CLI conflict guard.
- `capture/wiring.py` `CaptureBinding` / `.permutation` — a metrics-only mode that wires the
  proxy `persist=False` and exposes the per-permutation summaries instead of a file path.
- `suites/base.py` `_derive_wire` — when not persisting, roll up from the live summaries and
  **skip `write_transcript` entirely** (no bodies to render); `write_metrics` (numbers) may
  still write to the normal `<out>` (it is not capture data) or be embedded in the report.

## 5. Fail-loud + report behavior

- The report/`--help` must state, for a `--no-save-captures` run, that **captures were not
  persisted** (so an empty transcript section reads as intentional, not a bug) — Working
  Rule 8.
- The F1 `GateTally.blind()` warning is unaffected (it is memory-only) and still fires for
  an unrecognised dialect.
- `write_metrics`/`bench.json`/`provenance.json` carry only numbers/verdicts and continue to
  write to `<out>` — they are the report, which the user still wants. **Scope note:** this
  plan governs *model wire capture* (the proxy). Harness stdout/stderr and the oracle's
  `error_summary` are a different artifact (exec output for grading), not covered by
  `--no-save-captures`; left as-is (call this out explicitly so nobody assumes otherwise).

## 6. Relationship to F4 and the GV work

This **supersedes** the F4 temp-dir leak fix: once `--no-save-captures` creates no dir and
writes no bodies, there is no temp root to strand or to clean, so the `finally rmtree` and
the `capture_temp_root` bookkeeping are removed. F4's *CLI conflict guard*
(`--no-save-captures --capture-dir` → exit 2) **stays** — it is now even more clearly
correct (there is nothing to point a dir at). The GV3 slow tests
`test_gates_lifecycle.py` change from *"no residue after cleanup"* to the stronger
*"no `danno-bench-cap-*` dir is ever created"* (assert the mkdtemp path is never taken).

## 7. Tests

- **Unit (Tier A):** drive the real proxy with `persist=False` against a stub upstream;
  assert (a) the `GateTally` still ticks per F1, (b) `read_summaries()` returns body-free
  numbers, (c) **no file is created anywhere** — monkeypatch/observe `tempfile.mkdtemp`
  and `Path.open("a")`/`write_text` are never called, and the configured/temp dirs stay
  empty.
- **Parity:** `metrics_from_summaries(live)` == `metrics_from_files(equivalent capture)` for
  the same traffic, so the report is byte-identical regardless of the save flag.
- **No-body guarantee:** assert a `CallSummary` (and the whole in-memory list) contains no
  substring of a known prompt/completion fed through — a direct "prompts never retained"
  check.
- **Lifecycle (Tier B, GV3):** update `test_gates_lifecycle.py` — a completed AND a
  SIGINT-aborted `--no-save-captures` run leave **no capture dir at all** (stronger than the
  current residue check).

## 8. Milestones

- **N0** — `CallSummary` + `metrics_from_summaries` + proxy `persist` flag + live
  accumulation; Tier A unit tests (tally + no-file + parity). Save-mode behavior byte-
  unchanged. Gate green.
- **N1** — `_setup_bench_capture` metrics-only wiring under `--no-save-captures` (no mkdtemp);
  `_derive_wire` rolls up from summaries + skips the transcript; delete the F4 temp-root
  cleanup; report states "captures not persisted".
- **N2** — update GV3 `test_gates_lifecycle.py` to the "no dir ever created" assertion;
  README/`--help` wording; verify a real `--no-save-captures` bench writes zero capture
  bytes (config-is-code: exercise it, don't trust the flag).

## 9. Open questions

- **Q1 — metrics under `--no-save`:** keep full wire metrics in the report (recommended —
  derive live from summaries, strictly better than today), or is "persist nothing" meant to
  also drop the derived numbers? Numbers carry no prompt text, so keeping them seems right;
  confirm.
- **Q2 — unify save-mode metrics onto summaries too?** Rolling up *both* modes from the live
  summaries (and treating the on-disk JSONL as a pure archival artifact) removes the
  re-read-from-disk path (`metrics_from_files`) for the common case. Simpler, but changes
  save-mode's derivation source; decide at N0 whether to unify or keep disk-derivation for
  saved runs.
- **Q3 — relay path:** confirm the occ/claurst in-VM relay writes no captures of its own
  (it forwards to the proxy, which is the sole recorder). Verify at N1; if it does, it
  inherits the same `persist` gate.
