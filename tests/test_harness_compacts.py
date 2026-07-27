"""The `Harness.compacts` capability flag (P0 of the interactive-TUI plan).

`compacts` declares whether a harness performs usage-driven auto-compaction. The
interactive C-leg test (`tests/slow/tui`) branches on it: True → assert a
summarization request appears on the wire under inflated usage; False → assert
NONE appears (a change-detector). These fast assertions pin the registry values
so the flag can't silently drift — a flip is a conscious source edit here.
"""

from __future__ import annotations

from danno_validator.harnesses import all_names, get


def test_compacts_defaults_true() -> None:
    """The dataclass default is True, so a newly-registered harness is assumed to
    compact until proven otherwise (the safe direction: a real compaction that goes
    unasserted is a missed signal, not a false RED)."""
    from danno_validator.harnesses import Harness

    assert Harness.compacts is True  # dataclass field default


def test_claurst_declares_no_compaction() -> None:
    """claurst is the sole False row (spike 2026-07-21: no auto-compaction even at 2M
    tokens). This is the change-detector anchor."""
    assert get("claurst").compacts is False


def test_usage_driven_harnesses_compact() -> None:
    """codex + opencode arm usage-driven compaction (spike-proven on the wire)."""
    assert get("opencode").compacts is True
    assert get("codex").compacts is True


def test_every_registered_harness_has_a_bool_flag() -> None:
    """No harness leaves the capability unset (it defaults, but assert the type so a
    future bad override fails loud here rather than deep in a slow run)."""
    for name in all_names():
        assert isinstance(get(name).compacts, bool)
