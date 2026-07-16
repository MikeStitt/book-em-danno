# Review — PR #88 `feat(bench): runaway gates for danno bench (round/token/timeout)`

**Date:** 2026-07-14 · **Reviewer:** Claude (Fable 5), 8-angle inline review
(line-by-line / removed-behavior / cross-file tracer / reuse / simplification /
efficiency / altitude / conventions), recall-biased, findings verified against the
actual repo code (finder/verifier subagents were unavailable — org spend limit —
so all angles ran inline in one session).
**Scope:** `main...docs-bench-runaway-gates` (30 files, +1878/−133).
**Companions:** [`plan-bench-runaway-gates.md`](plan-bench-runaway-gates.md) (the
feature's DoR), [`plan-runaway-gates-validation.md`](plan-runaway-gates-validation.md)
(the long-lived validation scripts seeded by these findings — each F# maps to a
V# script there).

## Verdict in one paragraph

The design and its layering are solid: the three-gate reframe (round/token caps as
speed-invariant primaries, wall clock demoted to a wedged-process backstop), the pure
`GateTally` behind a structural `GateProbe` (keeping `core` below `capture`), the
Popen watchdog with pipe-drain reader threads, per-field gate resolution with
`extra="forbid"`, the option-B grace margin, the gate-kill-never-raises-
`CommandFailedError` distinction, and the in-VM reap fix are all well built and well
unit-tested. The findings are about the seams: **F1 undercuts the PR's central claim**
(a uniform external Gate-1 backstop), and F2/F3 are the watchdog inheriting the same
"can hang / can fail silent" character it was built to eliminate. The live
verification ([`live-verify-runaway-gates.md`](live-verify-runaway-gates.md)) covered
only opencode × Gate 3 — Gate 1 graceful-first, Gate 2, and the claurst/occ paths
have never fired live.

## Findings (most severe first)

### F1 — Gate 1/2 are usage-blind: silently inert for claurst-local cells — CONFIRMED (code-level)

`capture/proxy.py:160` feeds the tally only when `capture.usage.extract_usage`
returns a value, i.e. only for responses carrying a `usage` block. But claurst-local
dials the capture proxy (`_claurst_ollama_host` → `host.docker.internal:<capture_port>`)
with traffic that carries **no extractable `usage`**: Ollama-native `/api/chat`
bodies report `eval_count`/`prompt_eval_count` (no `usage` key, NDJSON — not
`data:`-prefixed SSE), and claurst's stream path is the known
`include_usage_in_stream` gap. Either way `extract_usage` returns `None`, the tally
never advances, and **Gate 1 and Gate 2 never fire** — while DoR §3.3 claims claurst
gets an external Gate-1 "✅ backstop at `+grace`" and §3.2 says Gate 1 "is uniform
across harnesses because the always-on proxy counts rounds for all of them."

*Failure scenario:* claurst (our own fork — every rebuild can change `--max-turns`
handling) regresses its native cap on a pathological model → the cell runs unbounded
until the loose 1800 s Gate 3; the promised `runaway` verdict never fires;
`wire.request_count` also reads 0, so the report under-states what happened.

*Suggested fix:* count **rounds by request path** (POST to `/v1/chat/completions` |
`/v1/responses` | `/v1/messages` | `/api/chat`), and use `usage` only for Gate 2's
token tally. Regression net: validation plan **V1** (dialect matrix, red rows
written first).

### F2 — Kill doesn't cover the host-side process tree; `reader.join()` can hang the watchdog itself — PLAUSIBLE

`core/exec.py:297` (`_capture_watched`) kills only the immediate child (no
`start_new_session=True`, no `os.killpg`), and the reader `join()`s at
`exec.py:305-306` have no timeout. If the `sbx`/`docker` CLI spawned a host-side
helper that inherited the stdout/stderr pipes, SIGKILLing the CLI does not close the
pipes → `proc.stdout.read()` never reaches EOF → `join()` blocks forever → `danno
bench` hangs *after* "killing" the cell — the exact D1 defect the PR exists to fix,
reintroduced one layer up.

*Suggested fix:* spawn with `start_new_session=True` and kill the process group;
and/or `join(timeout=...)` with a loud warning on expiry. Regression net: **V2**
(grandchild-holds-pipe row).

### F3 — Reader-thread exceptions are swallowed: watched execs fail quiet where unwatched execs fail loud — PLAUSIBLE

`core/exec.py:283-284`: the reader lambdas decode with `text=True` (strict locale
decoding). A harness emitting invalid UTF-8 raises `UnicodeDecodeError` *inside the
thread*; the exception dies with the thread, `out` stays `[]`, and
`out[0] if out else ""` (`exec.py:307`) silently returns empty stdout. The turn then
parses as empty and is misclassified with no error surfaced — a Working Rule 8
violation, and a behavioral divergence: the unwatched `subprocess.run(text=True)`
path raises in the main thread.

*Suggested fix:* `errors="replace"` (or capture and re-raise the reader exception).
Regression net: **V2** (invalid-UTF-8 emitter + watched/unwatched parity rows).

### F4 — `--no-save-captures` temp dir leaks on abort; `--capture-dir` silently ignored with it — CONFIRMED

The no-save branch creates `tempfile.mkdtemp(prefix="danno-bench-cap-")`
(`suites/bench.py:276`) but the cleanup (`bench.py:766`) runs only on the success
path at the end of `run_bench` — not in a `finally`. An aborted run (Ctrl-C,
provisioning error) leaves full wire captures — prompts included — in `/tmp`,
despite the user explicitly asking to persist nothing. Separately, the no-save
branch ignores `opts.capture_dir` without a warning, so
`--no-save-captures --capture-dir X` silently discards `X`.

*Suggested fix:* `try/finally` (or an ExitStack) around the run for the temp root;
warn or fail loud on the flag conflict. Regression net: **V4**.

### F5 — Provenance records the raw config, not the per-cell resolved gates; harness version still absent — CONFIRMED (drift)

`telemetry/provenance.py:170` dumps the `GatesConfig` once, but DoR §6 promises
"Provenance records the **resolved** gate values per cell." With per-model/
per-harness overrides in play, a cross-run comparison must re-run resolution against
the exact config to know a cell's caps. The harness *version* gap (stub-AI plan §8:
`harness_versions` records only the name; the sandbox template is unpinned, and the
opencode V1→V2 cutover changes `agent.steps` semantics) was flagged to "ride along
with M2" and is still open.

*Suggested fix:* record `resolved_gates` per verdict row (or per model×harness in
provenance), plus the harness version. Regression net: **V5**.

### F6 — README still documents old bench `--capture` semantics — CONFIRMED (Documentation Hygiene)

Constitution: "Any behavior-affecting change MUST update affected `--help` text,
READMEs, and related documentation in the same commit." The bench `--help` text was
updated, but `README.md:321` ("With `--capture`, `report.html` also plots per-cell
…") is now wrong for bench — capture is always on and `--capture` is a deprecated
no-op — and `--no-save-captures` appears nowhere in the README. (The
`sandbox start --capture` / `validate --capture` sections remain correct — those
paths are unchanged.)

### F7 — The reaper's `pkill -f` self-matches its own `bash -lc` wrapper — CONFIRMED (benign today)

`_REAP_PATTERN` (`suites/base.py:115`) is matched with `pkill -9 -f` inside
`bash -lc "pkill -9 -f 'opencode|claurst|…'"` — the wrapper bash's own command line
contains the pattern, so the reaper SIGKILLs its own `sbx exec` session (pkill
excludes only its own PID). It works today solely because the call is `check=False`;
the cost is a spurious 137 exit and a latent footgun if the reaper is ever checked
or extended.

*Suggested fix:* match process names (`pkill -x` per name) or exclude the wrapper.
Observed incidentally by **V3**'s post-kill invariants.

### F8 — `HARNESS_NAMES` is dead code — CONFIRMED

`suites/config.py:23` defines `HARNESS_NAMES = ("opencode", "claurst", "occ",
"claude")`; nothing references it (the `Literal` types in `BenchmarksConfig`/
`GatesConfig` are written out inline). Simplicity first: delete it, or actually
derive the Literals from one source if that was the intent — two parallel lists
will drift.

## Noted and deliberately not filed

- **Gate 2 sums per-call totals** (prompt context re-counted every round) — that is
  the correct *spend* semantic, matching the DoR.
- **`passed=True` can coexist with a gate verdict** (grading still runs after a
  kill) — deliberate per `BenchVerdict`'s "passed is the ground truth; verdict is
  the classification" contract.
- **Gate 3's clock is per-`capture()` call**, not per cell — moot today because
  every turn driver (`opencode_run`/`claude_run`/`claurst_run`/`occ_run`) makes
  exactly one `capture()` call inside the watch block; worth re-checking if a turn
  driver ever grows a second exec.
- **`resolve_gates` iterates `gates.harness.items()`** where a dict lookup would do
  — trivial style nit, folded into nothing.
- **opencode `steps` can be shadowed by markdown agent defs** (markdown beats the
  generated jsonc on conflict) — already a documented open check (stub-AI plan M1(b));
  the external kill is the real bound either way.

## Disposition

F1–F4 are fix-worthy before the gates are relied on in an unattended campaign; the
recommended vehicle is a follow-up branch stacked on PR #88 (validation plan §7 Q1 —
user's call), with **V1/V2 written red-first** so each fix lands against its
regression net. F5–F8 are ride-alongs. The full validation architecture, tiers, and
milestones live in [`plan-runaway-gates-validation.md`](plan-runaway-gates-validation.md).
