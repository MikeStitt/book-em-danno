"""Unit tests for the M2/M3 Claurst AUT seam — `install_claurst` command
construction and `authed_claurst_run` forwarding. No Docker daemon: the install is
asserted via the advised (non-apply) command, and the turn producer is checked by
stubbing `claurst.claurst_run`."""

from __future__ import annotations

from pathlib import Path

import pytest

from book_em_danno.core.exec import Runner
from danno_validator import claurst


def test_install_claurst_curl_fetches_release() -> None:
    # Runner() does not apply, so advise returns the command without executing it.
    cmd = claurst.install_claurst(Runner(), "box")
    assert cmd[:6] == ["docker", "sandbox", "exec", "box", "bash", "-lc"]
    script = cmd[6]
    assert "curl -fsSL" in script
    # Resume + retry survives the egress proxy truncating the CDN transfer (curl 18).
    assert "--retry-all-errors" in script
    assert "-C -" in script
    assert claurst.CLAURST_RELEASE_URL in script
    assert "npm" not in script  # npm's installer bypasses the proxy and fails
    assert "~/.local/bin/claurst" in script
    assert "command -v claurst" in script  # idempotent skip-if-present-and-working
    # Skip is version-gated: an existing sandbox on an older claurst is upgraded, not kept.
    assert f"grep -qF {claurst.CLAURST_VERSION}" in script
    assert "libasound2" in script  # claurst links ALSA; a clean shell VM lacks it


def test_install_claurst_release_url_pins_version() -> None:
    assert claurst.CLAURST_VERSION in claurst.CLAURST_RELEASE_URL
    assert claurst.CLAURST_RELEASE_URL.endswith("claurst-linux-aarch64.tar.gz")


def test_authed_claurst_run_binds_env_file_and_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_claurst_run(runner, name, prompt, **kw):  # type: ignore[no-untyped-def]
        captured.update(kw)
        captured["name"] = name
        captured["prompt"] = prompt
        return object()

    monkeypatch.setattr(claurst, "claurst_run", fake_claurst_run)
    run = claurst.authed_claurst_run(Path("/tmp/danno-env-xyz"))
    run(Runner(), "box", "do it", model="ollama/llama3.2:latest", workspace="/ws")
    assert captured["env_file"] == Path("/tmp/danno-env-xyz")
    assert captured["model"] == "ollama/llama3.2:latest"
    assert captured["workspace"] == "/ws"
    assert captured["name"] == "box"


def test_authed_claurst_run_allows_none_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        claurst, "claurst_run", lambda r, n, p, **kw: captured.update(kw) or object()
    )
    claurst.authed_claurst_run(None)(Runner(), "box", "hi")
    assert captured["env_file"] is None


def test_interactive_launch_script_default_relay_upstream() -> None:
    argv = claurst.interactive_launch_script("ollama/qwen3-coder-next", [])
    assert argv[:2] == ["bash", "-lc"]
    script = argv[2]
    assert "claurst -m ollama/qwen3-coder-next" in script
    assert "DANNO_RELAY_UPSTREAM_PORT=11434 python3" in script  # real Ollama by default


def test_interactive_launch_script_capture_port_redirects_relay() -> None:
    # --capture points the interactive session's relay at the recording proxy port.
    argv = claurst.interactive_launch_script("ollama/x", ["--foo"], capture_port=40404)
    script = argv[2]
    assert "DANNO_RELAY_UPSTREAM_PORT=40404 python3" in script
    assert "--foo" in script  # passthru args preserved
