"""Small user-owned TOML schemas for analysis policy and optional pricing."""

from __future__ import annotations

import math
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class CategoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[str] = Field(default_factory=list)


class RunGroup(BaseModel):
    """User assertion that source runs used one meaningful agent configuration."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    runs: list[Path] = Field(min_length=1)


class RecommendationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: Literal["reliability", "lowest_cost", "lowest_latency"] = "reliability"
    reliability_threshold: float = Field(default=0.0, ge=0.0, le=1.0)
    reliability_basis: Literal["lower_confidence_bound", "point_estimate"] = (
        "lower_confidence_bound"
    )
    maximum_cost_per_attempt: float | None = Field(default=None, ge=0.0)
    maximum_cost_per_success: float | None = Field(default=None, ge=0.0)
    maximum_p95_latency_s: float | None = Field(default=None, ge=0.0)
    allowed_harnesses: list[str] | None = None
    allowed_models: list[str] | None = None
    minimum_sample_count: int = Field(default=2, ge=1)
    minimum_distinct_tasks: int = Field(default=3, ge=1)


class AnalysisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    study: str = "danno-analysis"
    confidence_level: float = Field(default=0.95, gt=0.0, lt=1.0)
    statistics_seed: int = 1729
    recommendation: RecommendationConfig = Field(default_factory=RecommendationConfig)
    categories: dict[str, CategoryConfig] = Field(default_factory=dict)
    run_groups: list[RunGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_run_group_names(self) -> AnalysisConfig:
        names = [group.name for group in self.run_groups]
        if len(names) != len(set(names)):
            raise ValueError("run group names must be unique")
        return self


class ModelPrice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_per_million: float
    cached_input_per_million: float | None = None
    output_per_million: float

    @field_validator("input_per_million", "cached_input_per_million", "output_per_million")
    @classmethod
    def finite_nonnegative(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("pricing rates must be finite and non-negative")
        return value


class PricingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    effective_date: str
    currency: str = Field(min_length=1)
    models: dict[str, ModelPrice]


def _load_toml(path: Path) -> dict[str, object]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path}: invalid TOML — {exc}") from exc


def load_analysis_config(path: Path | None) -> AnalysisConfig:
    if path is None:
        return AnalysisConfig()
    try:
        config = AnalysisConfig.model_validate(_load_toml(path))
    except ValidationError as exc:
        raise ValueError(f"{path}: invalid analysis configuration — {exc}") from exc
    base = path.resolve().parent
    groups = [
        group.model_copy(
            update={
                "runs": [
                    run.resolve() if run.is_absolute() else (base / run).resolve()
                    for run in group.runs
                ]
            }
        )
        for group in config.run_groups
    ]
    seen: dict[Path, str] = {}
    for group in groups:
        for run in group.runs:
            if run in seen:
                raise ValueError(
                    f"{path}: run {run} belongs to multiple run groups: "
                    f"{seen[run]!r} and {group.name!r}"
                )
            seen[run] = group.name
    return config.model_copy(update={"run_groups": groups})


def load_pricing_config(path: Path | None) -> PricingConfig | None:
    if path is None:
        return None
    try:
        return PricingConfig.model_validate(_load_toml(path))
    except ValidationError as exc:
        raise ValueError(f"{path}: invalid pricing configuration — {exc}") from exc
