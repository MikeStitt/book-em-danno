"""Decisive in-sandbox wire-capture test (research doc §6 tests T1/T2/T3).

Provisions a real Docker sandbox, points its opencode at a recording proxy in
front of host Ollama, runs a one-shot `opencode run`, and asserts on the HTTP
body Ollama actually receives. Settles, at the wire:
  T1 — opencode forwards `reasoningEffort` -> body `reasoning_effort`; always
       streams; emits no inert `thinking`/`num_ctx`.
  T2 — with reasoning enabled (gemma4 returns a `reasoning` field) the sandboxed
       opencode build does NOT hang on #21903.
  T3 — the sandbox's opencode `--version` is recorded for provenance.

Skips when sbx or Ollama is down or gemma4:26b is absent. Never runs opencode
on the host — only via `sbx exec`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# tests/slow has no __init__.py, so under pytest's prepend import mode this dir is
# on sys.path and a plain import resolves the sibling helper.
from capture_proxy import capture_proxy, read_captures

from book_em_danno.commands import ollama, sandbox
from book_em_danno.config.generate import generate
from book_em_danno.config.schema import DannoConfig, Defaults, Model, OllamaBackend
from book_em_danno.core.exec import Runner

pytestmark = pytest.mark.slow

MODEL = "gemma4:26b"
PROXY_PORT = 11435
NAME = "danno-livetest-capture"


def _model_present(tag: str) -> bool:
    import json
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{ollama.DEFAULT_HOST_URL}/api/tags", timeout=2.0) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False
    return any(m.get("name") == tag for m in body.get("models", []))


ollama_down = not ollama.reachable()
model_absent = not _model_present(MODEL)
# This test drives the sandbox directly (not via the danno CLI) and speaks only `sbx`,
# so it needs `sbx` on PATH with its Docker runtime up; it skips cleanly otherwise.
sbx_down = shutil.which("sbx") is None or (
    subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0
)


def _config(*, reasoning: bool) -> DannoConfig:
    """A minimal config dialing the capture proxy (port 11435) instead of Ollama."""
    return DannoConfig(
        # default_agent must name a defined agent or opencode rejects the session.
        defaults=Defaults(default_agent="build"),
        backends={
            "ollama": OllamaBackend(
                kind="ollama",
                base_url=f"http://host.docker.internal:{PROXY_PORT}/v1",
            )
        },
        models={
            "gemma": Model(
                backend="ollama",
                tag=MODEL,
                reasoning_effort="none" if reasoning else None,
                context_budget=32000,
                output_limit=8192,
            )
        },
        agents={"build": "gemma"},
    )


def _chat_captures(capture_file: Path) -> list[dict[str, object]]:
    return [c for c in read_captures(capture_file) if c["path"].endswith("/chat/completions")]


def _teardown_sandbox(name: str) -> None:
    # sbx-only (this test provisions via sbx). `sbx rm` needs `--force` in a headless
    # shell and won't remove a running VM, so stop first. Best-effort.
    subprocess.run(["sbx", "stop", name], capture_output=True, check=False)
    subprocess.run(["sbx", "rm", "--force", name], capture_output=True, check=False)


def _run_opencode(target: Path, prompt: str, *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    trigger = f"cd {target} && opencode run -m ollama/{MODEL} {prompt!r}"
    return subprocess.run(
        ["sbx", "exec", NAME, "bash", "-lc", trigger],
        capture_output=True,
        check=False,
        timeout=timeout,
    )


@pytest.mark.skipif(
    sbx_down or ollama_down or model_absent,
    reason="sbx/Ollama down or gemma4:26b absent",
)
def test_opencode_wire_contract(tmp_path: Path) -> None:
    capture_file = tmp_path / "capture.jsonl"
    generate(_config(reasoning=True), tmp_path, apply=True)
    _teardown_sandbox(NAME)
    with capture_proxy(capture_file, port=PROXY_PORT):
        try:
            # The proxy rewrites host.docker.internal -> localhost before the sandbox
            # allow-list match, so the rules name localhost (both Ollama + proxy ports).
            sandbox.provision(
                Runner(apply=True),
                NAME,
                tmp_path,
                allow_hosts=("localhost:11434", f"localhost:{PROXY_PORT}"),
            )
            ver = subprocess.run(
                ["sbx", "exec", NAME, "opencode", "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
            version = ver.stdout.strip()
            assert version, f"could not read sandbox opencode --version: {ver.stderr}"

            # Run A (T1/T3): reasoning_effort="none" configured. `opencode run` emits
            # nothing on stdout when piped, so we assert on the captured wire body, not
            # its output.
            run_a = _run_opencode(tmp_path, "Reply with the single word: ping", timeout=600)
            chats = _chat_captures(capture_file)
            assert chats, (
                f"no /chat/completions captured (opencode build {version}): {run_a.stderr!r}"
            )
            body = chats[-1]["body"]
            assert body.get("reasoning_effort") == "none", body
            assert body.get("stream") is True, f"opencode should always stream: {body}"
            assert "thinking" not in body, body
            assert "num_ctx" not in body, body

            # Run B (T2, #21903 regression): no reasoning_effort -> gemma4 returns a
            # `reasoning` field; the sandboxed opencode build must not hang parsing it.
            # Success signal: a NEW chat round-trips (request captured) AND no timeout —
            # this also rules out the RC-0-but-never-called failure mode.
            generate(_config(reasoning=False), tmp_path, apply=True)
            before = len(chats)
            try:
                _run_opencode(tmp_path, "Reply with the single word: pong", timeout=600)
            except subprocess.TimeoutExpired:
                pytest.fail(
                    "sandboxed opencode hung with a reasoning field present — its build "
                    f"likely lacks the #21903 fix; keep reasoning_effort='none' for local "
                    f"models or update the sandbox's opencode (build: {version})"
                )
            after = _chat_captures(capture_file)
            assert len(after) > before, (
                f"Run B made no model round-trip (opencode build {version}); "
                "config likely rejected before the request"
            )
            # The reasoning-bearing run must NOT carry our suppression key.
            assert "reasoning_effort" not in after[-1]["body"], after[-1]["body"]
        finally:
            _teardown_sandbox(NAME)
