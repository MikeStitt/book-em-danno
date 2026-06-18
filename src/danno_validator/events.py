"""Progress events emitted by the tiered runner, for live status reporting.

`run_tiers` accepts an optional `on_event` callback and emits a `ValidateEvent` at
each config/tier boundary, so a CLI can render "what it is doing right now" without
the harness owning any console code. The callback is optional and defaults to None,
so the library sweep is unchanged when nobody is watching.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from danno_validator.oracle import FailureClass

Phase = Literal["config-start", "tier-start", "tier-done", "tier-skip", "config-done"]


@dataclass
class ValidateEvent:
    """One progress beat. `config`/`model_ref` identify the row; `level`/`label`
    the tier (0 liveness · 1 tool/bash · 2 software-dev). On `tier-done`, `overall`/
    `passed`/`latency_s`/`tokens` carry the verdict; on `tier-skip`, `reason` says
    why the short-circuit fired."""

    phase: Phase
    config: str
    model_ref: str
    level: int | None = None
    label: str = ""
    overall: FailureClass | None = None
    passed: bool = False
    latency_s: float = 0.0
    tokens: int = 0
    reason: str = ""


ProgressFn = Callable[[ValidateEvent], None]
