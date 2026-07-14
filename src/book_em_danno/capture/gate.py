"""`GateTally`: the live per-cell counters the runaway-gate watchdog polls.

`danno bench`'s capture proxy (`capture.proxy`) updates a `GateTally` as each model
response streams through — one `record()` per usage-bearing inference call. The watchdog
wrapping the harness exec (`core.exec.Runner`) reads the same tally to decide whether Gate
1 (round count) or Gate 2 (token spend) has tripped. See `.docs/plan-bench-runaway-gates.md`.

Structurally satisfies `core.exec.GateProbe` (`inference_calls()` / `tokens()`) without an
import, keeping the `core → capture` layering acyclic. Thread-safe: the proxy handles
requests on `ThreadingHTTPServer` worker threads while the watchdog polls from another.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class GateTally:
    """Cumulative inference-call count + token total for one bench cell."""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _calls: int = 0
    _tokens: int = 0

    def record(self, *, tokens: int | None) -> None:
        """Register one usage-bearing inference response. Non-inference traffic (no
        extractable `usage`, e.g. `/api/tags`) must NOT be recorded — the proxy calls
        this only when `capture.usage.extract_usage` returned a value, so the call count
        matches `wire_metrics.parse_capture_records` (Gate 1 = inference rounds, not
        discovery hits)."""
        with self._lock:
            self._calls += 1
            if tokens:
                self._tokens += tokens

    def inference_calls(self) -> int:
        with self._lock:
            return self._calls

    def tokens(self) -> int:
        with self._lock:
            return self._tokens
