"""Load and validate danno.toml. Fails loud (Working Rule 8)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import ValidationError

from .schema import DannoConfig


class DannoConfigError(Exception):
    """Raised when danno.toml is missing, malformed, or fails validation."""


def load_config(path: Path) -> DannoConfig:
    if not path.is_file():
        raise DannoConfigError(f"danno.toml not found: {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise DannoConfigError(f"invalid TOML in {path}: {exc}") from exc
    try:
        return DannoConfig.model_validate(raw)
    except ValidationError as exc:
        raise DannoConfigError(f"invalid danno.toml ({path}):\n{exc}") from exc
