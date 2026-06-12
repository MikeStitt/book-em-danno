"""Host-side sandbox registry (`~/.danno/sandboxes.json`).

Answers "which project is this sandbox?" and guards against two different target
paths colliding on one sandbox name. A thin, mockable wrapper (Working Rule 7):
the pure `load`/`lookup`/`record` take the json path explicitly so tests drive
them with `tmp_path` and never touch real host state.

Shape: `{name: {"target": <abs path>, "agent": <agent>}}`.
"""

from __future__ import annotations

import json
from pathlib import Path


def default_path() -> Path:
    """The host registry file danno uses outside of tests."""
    return Path.home() / ".danno" / "sandboxes.json"


def load(path: Path) -> dict[str, dict[str, str]]:
    """Read the registry; an absent or unreadable file is an empty registry."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def lookup(path: Path, name: str) -> dict[str, str] | None:
    """The recorded mapping for `name`, or None if it is unregistered."""
    return load(path).get(name)


def record(path: Path, name: str, target: str, agent: str) -> None:
    """Bind `name` → (target, agent). Idempotent: re-recording the same mapping
    leaves the file's content unchanged (keys are sorted on write)."""
    data = load(path)
    data[name] = {"target": target, "agent": agent}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
