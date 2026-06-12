from __future__ import annotations

from pathlib import Path

from book_em_danno.core import registry


def test_load_missing_is_empty(tmp_path: Path) -> None:
    assert registry.load(tmp_path / "nope.json") == {}


def test_record_then_lookup_round_trips(tmp_path: Path) -> None:
    reg = tmp_path / "sandboxes.json"
    registry.record(reg, "danno-work-acme", "/work/acme", "claude")
    assert registry.lookup(reg, "danno-work-acme") == {"target": "/work/acme", "agent": "claude"}
    assert registry.lookup(reg, "absent") is None


def test_record_is_idempotent(tmp_path: Path) -> None:
    reg = tmp_path / "sandboxes.json"
    registry.record(reg, "danno-x", "/x", "opencode")
    first = reg.read_text()
    registry.record(reg, "danno-x", "/x", "opencode")
    assert reg.read_text() == first  # same mapping = byte-identical file


def test_record_creates_parent_dir(tmp_path: Path) -> None:
    reg = tmp_path / "nested" / "dir" / "sandboxes.json"
    registry.record(reg, "danno-x", "/x", "opencode")
    assert reg.is_file()


def test_load_ignores_corrupt_file(tmp_path: Path) -> None:
    reg = tmp_path / "sandboxes.json"
    reg.write_text("not json{")
    assert registry.load(reg) == {}
