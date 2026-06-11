from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.commands import ollama, sandbox
from conftest import RecordingRunner


def test_default_name() -> None:
    assert sandbox.default_name(Path("/tmp/my-proj")) == "danno-my-proj"


def test_create_command(tmp_path: Path) -> None:
    r = RecordingRunner()
    sandbox.create(r, "probe", tmp_path)
    assert r.joined() == [f"docker sandbox create --name probe opencode {tmp_path}"]


def test_configure_proxy_opens_ollama_hole() -> None:
    r = RecordingRunner()
    sandbox.configure_proxy(r, "probe")
    assert r.joined() == [
        "docker sandbox network proxy probe --policy allow --allow-host localhost:11434"
    ]


def test_provision_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "loopback_warning", lambda **kw: None)
    r = RecordingRunner()
    sandbox.provision(r, "probe", tmp_path)
    assert r.joined() == [
        f"docker sandbox create --name probe opencode {tmp_path}",
        "docker sandbox network proxy probe --policy allow --allow-host localhost:11434",
        "docker sandbox stop probe",
    ]


def test_launch_builds_exec_command() -> None:
    r = RecordingRunner()
    sandbox.launch(r, "probe")
    assert r.joined() == ["docker sandbox exec -it --env-file <env-file> probe opencode"]


def test_shell_command() -> None:
    r = RecordingRunner()
    sandbox.shell(r, "probe")
    assert r.joined() == ["docker sandbox exec -it probe bash"]
