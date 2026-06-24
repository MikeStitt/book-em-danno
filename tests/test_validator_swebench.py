"""Unit tests for the M6 SWE-bench suite — row parsing, host-side HF fetch (paged,
mocked), and SwebenchTask provision/reset/grade command construction (in-VM
capture_exec stubbed). No network, no Docker."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from book_em_danno.core.exec import CaptureResult, Runner
from danno_validator.suites import swebench
from danno_validator.suites.swebench import fetch_instances, task_from_row

_ROW = {
    "instance_id": "demo__demo-1",
    "repo": "demo/demo",
    "base_commit": "abc123",
    "problem_statement": "Fix the widget.",
    "test_patch": "diff --git a/t_test.py b/t_test.py\n",
    "FAIL_TO_PASS": json.dumps(["t_test.py::test_a", "t_test.py::test_b"]),
    "PASS_TO_PASS": ["t_test.py::test_c"],  # already a list
}


def test_as_list_handles_json_string_list_and_plain() -> None:
    assert swebench._as_list(json.dumps(["a", "b"])) == ["a", "b"]
    assert swebench._as_list(["x"]) == ["x"]
    assert swebench._as_list("solo") == ["solo"]
    assert swebench._as_list(None) == []


def test_task_from_row_parses_fields() -> None:
    t = task_from_row(_ROW)
    assert t.id == "demo__demo-1"
    assert t.repo == "demo/demo"
    assert t.base_commit == "abc123"
    assert t.fail_to_pass == ("t_test.py::test_a", "t_test.py::test_b")
    assert t.pass_to_pass == ("t_test.py::test_c",)
    assert "Fix the widget." in t.prompt
    assert "do NOT edit any test files" in t.prompt


def _mock_rows_api(monkeypatch: pytest.MonkeyPatch, pages: list[list[dict]]) -> None:
    """Stub urlopen to serve `pages` of rows (one list per offset window)."""
    calls = {"n": 0}

    def fake_urlopen(url, timeout=0):  # type: ignore[no-untyped-def]
        idx = calls["n"]
        calls["n"] += 1
        rows = pages[idx] if idx < len(pages) else []
        body = json.dumps({"rows": [{"row": r} for r in rows]}).encode()
        return io.BytesIO(body)  # BytesIO is a context manager json.load can read

    monkeypatch.setattr(swebench.urllib.request, "urlopen", fake_urlopen)


def test_fetch_instances_pages_and_collects(monkeypatch: pytest.MonkeyPatch) -> None:
    other = {**_ROW, "instance_id": "demo__demo-2"}
    _mock_rows_api(monkeypatch, [[other], [_ROW]])  # target is on the 2nd page
    found = fetch_instances(["demo__demo-1"], dataset="d")
    assert set(found) == {"demo__demo-1"}
    assert found["demo__demo-1"]["repo"] == "demo/demo"


def test_fetch_instances_missing_id_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_rows_api(monkeypatch, [[_ROW]])
    with pytest.raises(ValueError, match="not found"):
        fetch_instances(["demo__demo-1", "ghost__ghost-9"], dataset="d")


def _stub_capture(monkeypatch: pytest.MonkeyPatch, *, grade_ok: bool) -> list[str]:
    seen: list[str] = []

    def fake(runner, name, command, *, check=False):  # type: ignore[no-untyped-def]
        seen.append(command)
        rc = 0 if (grade_ok or "pytest" not in command) else 1
        return CaptureResult([command], rc, "", "")

    monkeypatch.setattr(swebench, "capture_exec", fake)
    return seen


def test_provision_clones_checks_out_applies_patch_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _stub_capture(monkeypatch, grade_ok=True)
    t = task_from_row(_ROW)
    t.provision(Runner(), "box", tmp_path)
    script = seen[0]
    assert "github.com/demo/demo.git" in script
    assert "git checkout -f abc123" in script
    assert "git apply" in script
    assert "pip install" in script and "-e ." in script
    # VM-local checkout under /tmp (NOT the mounted workspace), patch via heredoc.
    assert "/tmp/danno-swe/demo__demo-1" in script
    assert "diff --git" in script  # the test patch is heredoc'd into the script
    assert not (tmp_path / "demo__demo-1").exists()  # nothing written to the host mount


def test_grade_runs_fail_and_pass_node_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _stub_capture(monkeypatch, grade_ok=True)
    t = task_from_row(_ROW)
    assert t.grade(Runner(), "box", tmp_path) is True
    cmd = seen[-1]
    assert "pytest" in cmd
    assert "t_test.py::test_a" in cmd and "t_test.py::test_c" in cmd


def test_grade_fail_when_tests_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_capture(monkeypatch, grade_ok=False)
    assert task_from_row(_ROW).grade(Runner(), "box", tmp_path) is False


def test_reset_restores_base_and_reapplies_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _stub_capture(monkeypatch, grade_ok=True)
    task_from_row(_ROW).reset(Runner(), "box", tmp_path)
    script = seen[0]
    assert "git checkout -f abc123" in script
    assert "git clean -fd" in script
    assert "git apply" in script and "/tmp/danno-swe/demo__demo-1.patch" in script


def test_offline_wheel_cache_uses_no_build_isolation() -> None:
    assert "--no-build-isolation" in task_from_row(_ROW, deps="offline-wheel-cache")._pip()
    assert "--no-build-isolation" not in task_from_row(_ROW, deps="no-cache-dir")._pip()
