from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.core import registry


def test_load_missing_is_empty(tmp_path: Path) -> None:
    assert registry.load(tmp_path / "nope.json") == {}


def test_record_then_lookup_round_trips(tmp_path: Path) -> None:
    reg = tmp_path / "sandboxes.json"
    registry.record(reg, "danno-work-acme", "/work/acme", "claude")
    assert registry.lookup(reg, "danno-work-acme") == {"target": "/work/acme", "harness": "claude"}
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


def test_load_ignores_corrupt_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # A present-but-corrupt registry is an anomaly, not an empty one: it must WARN (greppable)
    # before falling back to empty, else the name-collision guard silently goes blind (§5).
    reg = tmp_path / "sandboxes.json"
    reg.write_text("not json{")
    assert registry.load(reg) == {}
    err = " ".join(capsys.readouterr().err.split())
    assert "[WARNING]" in err and "unreadable" in err


def test_load_non_object_warns_and_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reg = tmp_path / "sandboxes.json"
    reg.write_text('["a", "list", "not", "an", "object"]')
    assert registry.load(reg) == {}
    assert "not a JSON object" in " ".join(capsys.readouterr().err.split())


def test_load_missing_is_silently_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # An ABSENT registry is legitimately empty — no warning, distinct from the corrupt case.
    assert registry.load(tmp_path / "nope.json") == {}
    assert capsys.readouterr().err == ""


def test_record_unwritable_path_fails_loud(tmp_path: Path) -> None:
    # A failed registry write is definitive (the name-collision guard goes blind), so it must
    # surface as a typed RegistryError, not a bare OSError traceback (policy §5, ERROR). Point
    # the registry at a path whose parent is a FILE so mkdir raises deterministically.
    (tmp_path / "afile").write_text("i am a file, not a dir")
    reg = tmp_path / "afile" / "sandboxes.json"
    with pytest.raises(registry.RegistryError, match="cannot write sandbox registry"):
        registry.record(reg, "danno-x", "/x", "opencode")
