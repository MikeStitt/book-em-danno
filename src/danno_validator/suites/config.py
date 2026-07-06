"""Benchmark-suite configuration: which suites are enabled and which tests run.

Kept OUT of the core `danno.toml` schema (a validator concern, not a provisioning
one): a separate `benchmarks.toml`, loaded only by `danno validate --benchmark`.
`extra="forbid"` so a typo fails loud at load (Working Rule 8). Each suite is
independently `enabled`, with a `select` list naming exactly which instances run —
the throttle on a matrix that is AUT x suite x tests x models.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

# Default file name looked up next to the project's danno.toml.
DEFAULT_BENCHMARKS_FILE = "benchmarks.toml"


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


class BenchmarksConfig(BaseModel):
    """The whole `benchmarks.toml`: one optional table per suite."""

    model_config = ConfigDict(extra="forbid")
    # Agents-under-test to sweep in one `danno bench` run (each gets its own bench.json +
    # sidecars under <out>/<agent>/, with a combined comparison report at the root). Empty
    # (the default) means the single opencode default; `--agent` on the CLI overrides this.
    # An unknown name fails loud at load (Working Rule 8).
    agents: list[Literal["opencode", "claurst", "occ", "claude"]] = []
    aider_polyglot: AiderPolyglotConfig = AiderPolyglotConfig()
    swebench: SwebenchConfig = SwebenchConfig()

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
