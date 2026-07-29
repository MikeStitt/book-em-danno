"""`GateTally`: the live per-cell counters the runaway-gate watchdog polls.

`danno bench`'s capture proxy (`capture.proxy`) updates a `GateTally` as each model
response streams through — one `record()` per inference round (Gate 1 counts by request
path, not by whether a `usage` block came back, so claurst-local Ollama-native traffic is
counted too — F1). The watchdog wrapping the harness exec (`core.exec.Runner`) reads the
same tally to decide whether Gate 1 (round count) or Gate 2 (token spend) has tripped. See
`.docs/plan-bench-runaway-gates.md` and `.docs/plan-runaway-gates-validation.md` §2.1.

The tally also observes every POST the proxy sees, so a cell that made inference requests
but ticked zero rounds (`blind()`) — an unrecognised wire dialect — can be flagged loud
rather than silently under-counted.

Structurally satisfies `core.exec.GateProbe` (`inference_calls()` / `tokens()`) without an
import, keeping the `core → capture` layering acyclic. Thread-safe: the proxy handles
requests on `ThreadingHTTPServer` worker threads while the watchdog polls from another.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class GateTally:
    """Cumulative inference-round count + token total for one bench cell."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _calls: int = 0
    _tokens: int = 0
    _posts: int = 0
    # Per-backend POST tallies, keyed by the capture target's `backend_name`. The cell's proxies
    # share one tally (so the gates see the combined totals), so `_posts` alone can't tell whether
    # the MODEL's own backend was ever dialed — a cell can rack up POSTs on the idle local backend
    # (incidental title-gen) while its active cloud backend saw zero. This map keeps the split so
    # `posts(backend=...)` can answer "did the active backend see any traffic?" (#105).
    _posts_by_backend: dict[str, int] = field(default_factory=dict)

    def record(self, *, tokens: int | None) -> None:
        """Register one inference round. The proxy calls this for every POST to an
        inference endpoint (`capture.usage.is_inference_request`), whether or not the
        response carried token counts — so Gate 1 matches
        `wire_metrics.parse_capture_records` (inference rounds, not discovery hits) across
        every dialect. `tokens` is `None` for a usage-less round (still a round; no token
        spend recorded)."""
        with self._lock:
            self._calls += 1
            if tokens:
                self._tokens += tokens

    def observe_post(self, backend: str | None = None) -> None:
        """Note that the proxy saw a POST (any path). Feeds `blind()` so an inference cell
        whose dialect the sensor didn't recognise is surfaced, not silently under-counted.
        `backend` (the capture target's `backend_name`) attributes the POST to a specific
        backend so `posts(backend=...)` can tell whether the active backend was dialed (#105)."""
        with self._lock:
            self._posts += 1
            if backend is not None:
                self._posts_by_backend[backend] = self._posts_by_backend.get(backend, 0) + 1

    def inference_calls(self) -> int:
        with self._lock:
            return self._calls

    def tokens(self) -> int:
        with self._lock:
            return self._tokens

    def posts(self, backend: str | None = None) -> int:
        """POSTs the proxy saw: the cell total (`backend=None`) or the count attributed to one
        `backend_name`. `posts(active_backend) == 0` means the model's own backend was never
        dialed — a 0-request cell (#105), distinct from `blind()` (traffic seen, none counted)."""
        with self._lock:
            return self._posts if backend is None else self._posts_by_backend.get(backend, 0)

    def blind(self) -> bool:
        """True when the proxy saw ≥1 POST but counted zero inference rounds — the gate
        sensor did not recognise this cell's wire dialect (Gates 1/2 were inert)."""
        with self._lock:
            return self._posts > 0 and self._calls == 0
