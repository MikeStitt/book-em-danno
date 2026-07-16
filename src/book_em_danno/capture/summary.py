"""`CallSummary`: the body-free numeric carrier the capture proxy accumulates in RAM.

Under `--no-save-captures` danno must write NO capture bytes to disk, yet the report still
wants token/latency/context numbers and the runaway gates still need their live tally. Both
run on *derived numbers*, never message text — so the proxy computes one `CallSummary` per
completed call (routing + timestamps + normalized `usage`, no prompt/completion text) and
keeps a list of them in memory. `danno_validator.telemetry.wire_metrics` maps each to a
`RequestMetric`, so the report is identical whether it was derived from these live summaries
or from the on-disk JSONL (the save-mode path).

Lives at the `capture` layer (pure stdlib) so the proxy can build it without importing the
validator — the acyclic `core/capture → validator` layering, the same reason `GateTally` is
structural.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CallSummary:
    """One proxied call reduced to numbers + routing — never any message body.

    `usage` is the normalized `{prompt, completion, total, cached}` dict (`capture.usage`)
    or `None` for a call that carried no extractable token counts. `ts_request`/`ts_response`
    are epoch seconds (their delta is the call's round-trip time). `method`/`path` let the
    metrics roll-up decide inclusion with `capture.usage.is_inference_request` — exactly as
    the on-disk parser does — so a summary-derived turn counts the same rounds the Gate-1
    tally did.
    """

    seq: int
    method: str
    path: str
    ts_request: float
    ts_response: float
    usage: dict[str, int | None] | None
