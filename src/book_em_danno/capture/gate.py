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

    def observe_post(self) -> None:
        """Note that the proxy saw a POST (any path). Feeds `blind()` so an inference cell
        whose dialect the sensor didn't recognise is surfaced, not silently under-counted."""
        with self._lock:
            self._posts += 1

    def inference_calls(self) -> int:
        with self._lock:
            return self._calls

    def tokens(self) -> int:
        with self._lock:
            return self._tokens

    def blind(self) -> bool:
        """True when the proxy saw ≥1 POST but counted zero inference rounds — the gate
        sensor did not recognise this cell's wire dialect (Gates 1/2 were inert)."""
        with self._lock:
            return self._posts > 0 and self._calls == 0
