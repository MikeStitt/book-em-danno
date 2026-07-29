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
    # `is_file()` is a TOCTOU snapshot; the read can still fail (permissions, a vanished
    # file, a non-UTF-8 payload). Those must surface through the DannoConfigError contract
    # too — never as a raw OSError/UnicodeDecodeError traceback (policy §5, FATAL).
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DannoConfigError(f"cannot read danno.toml ({path}): {exc}") from exc
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise DannoConfigError(f"invalid TOML in {path}: {exc}") from exc
    try:
        return DannoConfig.model_validate(raw)
    except ValidationError as exc:
        raise DannoConfigError(f"invalid danno.toml ({path}):\n{exc}") from exc
