"""Fixtures for the interactive TUI tests: capture the REAL launch argv, read the wire.

Reuses the gate fixtures unchanged (`tests/slow/gates_fixtures.py`) — `PROXY_PORT`,
`scripted_backend` (stub + capture proxy + tally), `provisioned_sandbox` (proxy-dialing config
+ real sandbox, egress-allowing ONLY `localhost:<PROXY_PORT>`). What this module adds
(`.docs/plan-slow-sandbox-tui-tests.md` §8.3):

- **`_CaptureLaunchRunner`** — a real `Runner` whose interactive-launch `run()` CAPTURES the
  argv+env instead of exec'ing it, so a host-pty driver can drive danno's *real* `sbx exec -it
  … <argv>` frame. Every other `run` is real (provision, config emit, etc.).
- **`launch_argv`** — builds that launch tuple via the real `sandbox.launch`.
- **`codex_compact_graft`** — prepends a small top-level `model_auto_compact_token_limit` to
  codex's `config.toml` (a codex-only requirement to compact under the inflated usage).
- **`WireMetrics`** — a thin reader over the capture JSONL unifying the two spike wire summaries
  behind one shape (`requests`/`summarization_requests`/`item_counts`).
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from gates_fixtures import PROXY_PORT

from book_em_danno.commands import sandbox
from book_em_danno.core.exec import Runner

from .primitives import HarnessTui


class _CaptureLaunchRunner(Runner):
    """Real `Runner`, except the interactive-launch `run()` captures (argv, env) instead of
    exec'ing — the host-pty driver drives that argv itself. Mirrors the spike interceptor."""

    def __init__(self) -> None:
        super().__init__(apply=True)
        self.launch_cmd: list[str] | None = None
        self.launch_env: dict[str, str] | None = None

    def run(self, cmd, why, *, cwd=None, env=None, check=True):  # type: ignore[no-untyped-def]
        if why.startswith("launch"):
            self.launch_cmd = list(cmd)
            self.launch_env = dict(env) if env else None
            return cmd
        return super().run(cmd, why, cwd=cwd, env=env, check=check)


def launch_argv(
    harness: str, cfg: HarnessTui, *, name: str, target: Path
) -> tuple[str, list[str], dict[str, str]]:
    """The real interactive launch as `(exe, args, env)`: run `sandbox.launch` under the
    capturing runner (it builds the exact `sbx exec -it … <argv>` a human's `danno sandbox
    start` would), then resolve `exe` on PATH and force a real `TERM` (sbx/pexpect refuse
    `dumb`). `env` merges the process env with any launch-forwarded overrides (issue #99)."""
    runner = _CaptureLaunchRunner()
    sandbox.launch(
        runner, name, target, harness=harness, capture_relay_port=PROXY_PORT, model=cfg.model
    )
    cmd = runner.launch_cmd
    assert cmd, "sandbox.launch did not issue a 'launch' run — nothing captured"
    env = {**os.environ, **(runner.launch_env or {})}
    if env.get("TERM", "") in ("", "dumb"):
        env["TERM"] = "xterm-256color"
    exe = shutil.which(cmd[0]) or cmd[0]
    return exe, list(cmd[1:]), env


@contextmanager
def codex_compact_graft(limit: int = 200) -> Iterator[None]:
    """Prepend a small top-level `model_auto_compact_token_limit` to codex's generated
    `config.toml` so it compacts under the inflated usage (spike monkeypatch 2). The key MUST
    sit above any `[model_providers.*]` header — prepending puts it top-level. codex-only;
    entered around provision + launch so the graft is live when the toml is written."""
    import book_em_danno.config.generate as gen

    orig = gen.codex_config_toml

    def patched(base_url: str, **kw: object) -> str:  # type: ignore[no-untyped-def]
        return f"model_auto_compact_token_limit = {limit}\n" + orig(base_url, **kw)  # type: ignore[arg-type]

    gen.codex_config_toml = patched  # type: ignore[assignment]
    try:
        yield
    finally:
        gen.codex_config_toml = orig  # type: ignore[assignment]


@dataclass
class WireMetrics:
    """A thin reader over the capture JSONL, keyed on the harness's `wire_path`. Unifies the two
    spike `wire_summary` variants (CHAT `messages` / RESPONSES `input`) behind one shape."""

    capture_file: Path
    cfg: HarnessTui

    def _records(self) -> list[dict]:
        recs: list[dict] = []
        if not self.capture_file.is_file():
            return recs
        for line in self.capture_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("direction") != "request" or rec.get("method") != "POST":
                continue
            if not str(rec.get("path", "")).endswith(self.cfg.wire_path):
                continue
            recs.append(rec)
        return recs

    @staticmethod
    def _body(rec: dict) -> dict:
        body = rec.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except json.JSONDecodeError:
                return {}
        return body if isinstance(body, dict) else {}

    def requests(self) -> int:
        """POST count to the harness's inference endpoint (the H-leg signal)."""
        return len(self._records())

    def item_counts(self) -> list[int]:
        """Per-request carried-history length (`messages` for CHAT, `input` for RESPONSES) — a
        real compaction SHRINKS this."""
        out: list[int] = []
        for rec in self._records():
            body = self._body(rec)
            items = body.get("messages") or body.get("input") or []
            out.append(len(items) if isinstance(items, list) else 0)
        return out

    def summarization_requests(self) -> int:
        """Count of requests whose body carries any of the harness's compaction markers (the
        C-leg signal). Whole-body scan — the markers are distinctive per harness."""
        n = 0
        for rec in self._records():
            blob = json.dumps(self._body(rec)).lower()
            if any(m in blob for m in self.cfg.summ_markers):
                n += 1
        return n
