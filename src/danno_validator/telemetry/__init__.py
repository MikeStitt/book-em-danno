"""Bench telemetry: host-side resource sampling and wire-capture metrics.

These modules turn a `danno bench` run into a profile — CPU/GPU/memory/VRAM over
each turn (`sampler`) and per-request token/context/latency series derived from the
`--capture` wire JSONL (`wire_metrics`). Everything here is opt-in and degrades
gracefully off the Linux/NVIDIA bench host; nothing is Apple-specific.
"""

from __future__ import annotations
