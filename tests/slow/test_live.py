"""Slow, opt-in live tests. Run with `pytest -m slow`.

These talk to the REAL host Ollama and Docker. They skip cleanly when the daemon
is down or Ollama is unreachable, so the gate stays green on a cold host. They
NEVER invoke host `opencode` — OpenCode only ever runs in-container, asserted via
`docker sandbox exec`.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from book_em_danno.commands import ollama

pytestmark = pytest.mark.slow

# Real tags pulled on this host (see resume-phase2-execution memory).
TOOL_CAPABLE_MODEL = "gemma4:26b"

ollama_down = not ollama.reachable()
docker_down = shutil.which("docker") is None or (
    subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0
)


@pytest.mark.skipif(ollama_down, reason="Ollama not reachable (start: OLLAMA_HOST=0.0.0.0 serve)")
def test_model_responds() -> None:
    assert ollama.verify_responds(TOOL_CAPABLE_MODEL)


@pytest.mark.skipif(ollama_down, reason="Ollama not reachable")
def test_model_can_tool_call() -> None:
    assert ollama.tool_call_probe(TOOL_CAPABLE_MODEL), (
        f"{TOOL_CAPABLE_MODEL} did not emit tool_calls — it is unusable for ADOS agents"
    )


@pytest.mark.skipif(docker_down, reason="Docker daemon down")
def _teardown_sandbox(name: str) -> None:
    # `docker sandbox rm` has no force flag and won't remove a running VM, so stop
    # first, then remove. Both are best-effort (the sandbox may not exist).
    subprocess.run(["docker", "sandbox", "stop", name], capture_output=True, check=False)
    subprocess.run(["docker", "sandbox", "rm", name], capture_output=True, check=False)


@pytest.mark.skipif(docker_down, reason="Docker daemon down")
def test_opencode_only_runs_in_container() -> None:
    # A throwaway sandbox proves OpenCode is reachable in-container — never on host.
    name = "danno-livetest"
    _teardown_sandbox(name)
    try:
        created = subprocess.run(
            ["docker", "sandbox", "create", "--name", name, "opencode", "."],
            capture_output=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr
        ver = subprocess.run(
            ["docker", "sandbox", "exec", name, "opencode", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert ver.returncode == 0 and ver.stdout.strip()
    finally:
        _teardown_sandbox(name)


@pytest.mark.skipif(docker_down, reason="Docker daemon down")
def test_claudecode_runs_in_container() -> None:
    # Claude Code is a Docker prebuilt agent like opencode; prove it runs
    # in-container. Auth-free (`--version` only) so it's green on a cold host.
    name = "danno-claudetest"
    _teardown_sandbox(name)
    try:
        created = subprocess.run(
            ["docker", "sandbox", "create", "--name", name, "claude", "."],
            capture_output=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr
        ver = subprocess.run(
            ["docker", "sandbox", "exec", name, "claude", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert ver.returncode == 0 and ver.stdout.strip()
    finally:
        _teardown_sandbox(name)
