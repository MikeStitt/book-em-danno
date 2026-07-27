"""Unit tests for the Phase-3 Codex HUT seam — `install_codex` command construction,
`interactive_launch_script` (inline config.toml + interactive codex), and
`authed_codex_run` forwarding. No Docker daemon: the install is asserted via the advised
(non-apply) command, and the turn producer is checked by stubbing `driver.codex_run`."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.core.exec import Runner
from danno_validator import codex


def test_install_codex_npm_and_pins_version() -> None:
    # Runner() does not apply, so exec_in_container returns the advised command unexecuted.
    cmd = codex.install_codex(Runner(), "box")
    assert cmd[:6] == ["docker", "sandbox", "exec", "box", "bash", "-lc"]
    script = cmd[6]
    assert f"npm install -g {codex.CODEX_NPM_PKG}@{codex.CODEX_VERSION}" in script
    # Idempotent skip: present AND exactly the pinned version (codex --version is reliable).
    assert "command -v codex" in script
    assert f'= "{codex.CODEX_VERSION}" ]' in script


def test_interactive_launch_script_writes_config_and_launches() -> None:
    argv = codex.interactive_launch_script("gpt-oss:20b", [])
    assert argv[:2] == ["bash", "-lc"]
    script = argv[2]
    # Inline config.toml into a VM-local CODEX_HOME, then interactive codex (no `exec`).
    assert "export CODEX_HOME=$HOME/.codex-danno" in script
    assert 'wire_api = "responses"' in script
    assert "http://host.docker.internal:11434/v1" in script
    assert "-m gpt-oss:20b" in script
    assert "codex exec" not in script  # interactive TUI, not headless exec


def test_interactive_launch_script_capture_dials_recording_proxy() -> None:
    argv = codex.interactive_launch_script("gpt-oss:20b", ["--foo"], capture_port=40404)
    script = argv[2]
    assert "http://host.docker.internal:40404/v1" in script
    assert "http://host.docker.internal:11434/v1" not in script
    assert "--foo" in script  # passthru args preserved


def test_interactive_launch_script_no_model_omits_flag() -> None:
    script = codex.interactive_launch_script(None, [])[2]
    assert "-m " not in script


def test_authed_codex_run_binds_env_file_and_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_codex_run(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        captured.update(kw)
        captured["name"] = name
        captured["prompt"] = prompt
        return object()

    monkeypatch.setattr(codex, "codex_run", fake_codex_run)
    run = codex.authed_codex_run(Path("/tmp/danno-env-xyz"), capture_port=1234)
    run(Runner(), "box", "do it", model="gpt-oss:20b", workspace="/ws")
    assert captured["env_file"] == Path("/tmp/danno-env-xyz")
    assert captured["capture_port"] == 1234
    assert captured["model"] == "gpt-oss:20b"
    assert captured["workspace"] == "/ws"
    assert captured["name"] == "box"


def test_authed_codex_run_allows_none_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(codex, "codex_run", lambda r, n, p, **kw: captured.update(kw) or object())
    codex.authed_codex_run(None)(Runner(), "box", "hi")
    assert captured["env_file"] is None


def test_authed_codex_run_model_override_replaces_caller_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # bench reports the generic `ollama/<tag>` ref, but codex's `-m` takes the bare tag
    # supplied as model_override (dial_ref strips the prefix).
    captured: dict[str, object] = {}
    monkeypatch.setattr(codex, "codex_run", lambda r, n, p, **kw: captured.update(kw) or object())
    run = codex.authed_codex_run(None, model_override="gpt-oss:20b")
    run(Runner(), "box", "go", model="ollama/gpt-oss:20b")
    assert captured["model"] == "gpt-oss:20b"
