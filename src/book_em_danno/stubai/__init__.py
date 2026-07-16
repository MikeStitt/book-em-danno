"""Deterministic stub AI for validating danno's runaway gates and exec watchdog.

A scripted model backend (`server.stub_ai`) plus its step vocabulary and wire framing
(`script`), used to reproduce gate behavior without a live model. See
`.docs/plan-runaway-gates-validation.md` and `.docs/plan-stub-ai-test-harness.md`.
"""

from __future__ import annotations

from book_em_danno.stubai.script import Drip, Finish, ToolCall, ToolLoop
from book_em_danno.stubai.server import Stub, StubConfig, stub_ai

__all__ = [
    "Drip",
    "Finish",
    "Stub",
    "StubConfig",
    "ToolCall",
    "ToolLoop",
    "stub_ai",
]
