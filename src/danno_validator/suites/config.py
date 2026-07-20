"""Benchmark-suite configuration: which suites are enabled and which tests run.

Kept OUT of the core `danno.toml` schema (a validator concern, not a provisioning
one): a separate `benchmarks.toml`, loaded only by `danno validate --benchmark`.
`extra="forbid"` so a typo fails loud at load (Working Rule 8). Each suite is
independently `enabled`, with a `select` list naming exactly which instances run —
the throttle on a matrix that is HUT x suite x tests x models.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from danno_validator import harnesses

# Default file name looked up next to the project's danno.toml.
DEFAULT_BENCHMARKS_FILE = "benchmarks.toml"


def _validate_harness_name(name: str) -> str:
    """Fail loud on a `benchmarks.toml` harness name the registry doesn't know (Working
    Rule 8). The valid set is the live registry (`harnesses.all_names()`), so adding a
    harness needs no edit here — the single source of truth is the registry itself."""
    valid = harnesses.all_names()
    if name not in valid:
        raise ValueError(f"unknown harness '{name}'. Valid harnesses: {', '.join(valid)}.")
    return name


class AiderPolyglotConfig(BaseModel):
    """Aider Polyglot: self-contained multi-language exercises (stub + test file)."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    # Shared sandbox + per-exercise workspace reset (exercises are self-contained);
    # `per-test` provisions a fresh sandbox per exercise for stronger isolation.
    isolation: Literal["shared", "per-test"] = "shared"
    source: str = "git:https://github.com/Aider-AI/polyglot-benchmark"
    select: list[str] = []  # exercise ids (e.g. "python/anagram"); empty = none


class SwebenchConfig(BaseModel):
    """SWE-bench Verified subset: real GitHub issues, graded by FAIL_TO_PASS tests."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    # Per-instance sandbox (each instance has its own repo + dep tree); `shared`
    # reuses one sandbox (only safe for instances with disjoint, compatible deps).
    isolation: Literal["per-test", "shared"] = "per-test"
    dataset: str = "princeton-nlp/SWE-bench_Verified"
    # Dependency install strategy in the proxy-only sandbox (M0 spike): a
    # curl-fetched offline wheel cache (default, robust) or pip --no-cache-dir.
    deps: Literal["offline-wheel-cache", "no-cache-dir"] = "offline-wheel-cache"
    select: list[str] = []  # instance ids (e.g. "django__django-11099"); empty = none


class GateLimits(BaseModel):
    """One layer of runaway-gate limits (`danno bench` per-cell caps). Every field is
    optional so an override layer sets only what it changes; a cell's effective caps are
    resolved by overlaying model > harness > global, per field (`resolve_gates`). See
    `.docs/plan-bench-runaway-gates.md`."""

    model_config = ConfigDict(extra="forbid")
    max_turns: int | None = None  # Gate 1 — inference calls per cell
    max_tokens: int | None = None  # Gate 2 — total tokens per cell
    timeout_s: float | None = None  # Gate 3 — wall-clock backstop (seconds)


class GatesConfig(GateLimits):
    """`[gates]` in `benchmarks.toml`: zero-thought global defaults (the base fields,
    which carry concrete values) plus optional per-harness / per-model overrides. The
    per-model key is the `danno.toml [models]` name. Defaults are runaway BACKSTOPS, not
    fairness normalizers — high enough that no legitimate solve hits them (DoR §7 D2)."""

    max_turns: int | None = 50
    max_tokens: int | None = 2_000_000
    timeout_s: float | None = 1800.0
    harness: dict[str, GateLimits] = {}
    model: dict[str, GateLimits] = {}

    @field_validator("harness")
    @classmethod
    def _check_harness_keys(cls, value: dict[str, GateLimits]) -> dict[str, GateLimits]:
        for name in value:
            _validate_harness_name(name)
        return value


@dataclass(frozen=True)
class ResolvedGates:
    """A single cell's effective gate caps after overlaying model > harness > global.
    A `None` field means that gate is disabled for the cell (the watchdog skips it)."""

    max_turns: int | None
    max_tokens: int | None
    timeout_s: float | None


def resolve_gates(gates: GatesConfig, *, harness: str, model: str | None) -> ResolvedGates:
    """The effective caps for one `(harness, model)` cell. Precedence is per field and
    independent: `[gates.model.<model>]` > `[gates.harness.<harness>]` > `[gates]`. A
    layer that leaves a field unset (`None`) falls through to the next; the global layer's
    defaults are the floor."""
    layers: list[GateLimits] = []
    if model is not None and model in gates.model:
        layers.append(gates.model[model])
    for name, override in gates.harness.items():
        if name == harness:
            layers.append(override)
    layers.append(gates)  # global defaults (the GateLimits base fields)

    def pick(attr: str) -> object:
        for layer in layers:
            value = getattr(layer, attr)
            if value is not None:
                return value
        return None

    return ResolvedGates(
        max_turns=pick("max_turns"),  # type: ignore[arg-type]
        max_tokens=pick("max_tokens"),  # type: ignore[arg-type]
        timeout_s=pick("timeout_s"),  # type: ignore[arg-type]
    )


# Grace margin for Gate 1 (`.docs/plan-bench-runaway-gates.md` §3.2, option B): the harness's
# own `--max-turns` / `agent.steps` is set to the resolved `max_turns` (the graceful target),
# while the external watchdog kills only at `max_turns + grace`. So a cap-honoring harness
# (claurst) stops itself cleanly FIRST, and the kill fires only for a harness that
# overshoots — opencode's advisory `agent.steps`, a true runaway, or step-vs-inference-call drift.
_GATE_TURN_GRACE_MIN = 3
_GATE_TURN_GRACE_FRAC = 0.1


def watchdog_max_turns(max_turns: int | None) -> int | None:
    """The external Gate-1 hard-kill round limit for a cell: the native polite-stop
    `max_turns` plus a grace margin of `max(3, 10%)`. `None` (Gate 1 disabled) stays `None`."""
    if max_turns is None:
        return None
    return max_turns + max(_GATE_TURN_GRACE_MIN, round(max_turns * _GATE_TURN_GRACE_FRAC))


class BenchmarksConfig(BaseModel):
    """The whole `benchmarks.toml`: one optional table per suite."""

    model_config = ConfigDict(extra="forbid")
    # Harnesses-under-test to sweep in one `danno bench` run (each gets its own bench.json +
    # sidecars under <out>/<harness>/, with a combined comparison report at the root). Empty
    # (the default) means the single opencode default; `--harness` on the CLI overrides this.
    # An unknown name fails loud at load (Working Rule 8).
    harnesses: list[str] = []
    gates: GatesConfig = GatesConfig()
    aider_polyglot: AiderPolyglotConfig = AiderPolyglotConfig()
    swebench: SwebenchConfig = SwebenchConfig()

    @field_validator("harnesses")
    @classmethod
    def _check_harnesses(cls, value: list[str]) -> list[str]:
        for name in value:
            _validate_harness_name(name)
        return value

    def any_enabled(self) -> bool:
        return self.aider_polyglot.enabled or self.swebench.enabled


def load_benchmarks(path: Path) -> BenchmarksConfig:
    """Load and validate a `benchmarks.toml`. A missing file yields the all-disabled
    default (benchmarks are opt-in). Malformed TOML or an unknown key fails loud."""
    if not path.is_file():
        return BenchmarksConfig()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: invalid TOML — {exc}") from exc
    try:
        return BenchmarksConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"{path}: invalid benchmarks config — {exc}") from exc
