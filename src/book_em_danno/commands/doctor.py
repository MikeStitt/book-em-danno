"""Read-only preflight (`danno doctor`). Port of `tools/ados-ollama-doctor`.

PASS/FAIL/WARN checklist with copy-paste fixes. Changes nothing. No host
`opencode` check — OpenCode only ever runs in the sandbox (see commands/sandbox).
Returns the number of failed REQUIRED checks (0 = healthy) so the CLI can set the
exit code.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from ..core.exec import console
from . import ollama, sandbox_cli

MIN_PYTHON = (3, 13)


@dataclass
class _Tally:
    failed: int = 0
    warned: int = 0


def _report(tally: _Tally, *, required: bool, label: str, fix: str, ok: bool) -> None:
    if ok:
        console.print(f"  [green]PASS[/green]  {label}")
        return
    if required:
        tally.failed += 1
        console.print(f"  [red]FAIL[/red]  {label}")
    else:
        tally.warned += 1
        console.print(f"  [yellow]WARN[/yellow]  {label}")
    console.print(f"        fix: {fix}")


def _cmd_ok(*cmd: str) -> bool:
    try:
        return (
            subprocess.run(
                list(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
            ).returncode
            == 0
        )
    except (FileNotFoundError, OSError):
        return False


def _on_path(name: str) -> bool:
    return shutil.which(name) is not None


def run_doctor(*, ollama_host_url: str = ollama.DEFAULT_HOST_URL) -> int:
    """Run the checklist, print it, and return the count of failed required checks."""
    tally = _Tally()
    console.print("danno doctor — preflight\n")
    console.print("Runtime (required to provision and run the sandbox):")

    checks: list[tuple[bool, str, str, Callable[[], bool]]] = [
        (
            True,
            f"Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            "install a newer Python (e.g. via uv or python.org)",
            lambda: sys.version_info[:2] >= MIN_PYTHON,
        ),
        (
            True,
            "git on PATH",
            "install git (xcode-select --install, or brew install git)",
            lambda: _on_path("git"),
        ),
        (True, "Docker daemon running", "start Docker Desktop", lambda: _cmd_ok("docker", "info")),
        (
            True,
            f"sandbox CLI ({sandbox_cli.label()})",
            "install sbx (brew install docker/tap/sbx) or update Docker Desktop",
            lambda: _cmd_ok(*sandbox_cli.availability_argv()),
        ),
        (
            True,
            "ollama installed",
            "brew install ollama  (or see https://ollama.com)",
            lambda: _on_path("ollama"),
        ),
        (
            True,
            "Ollama server reachable",
            "start it: OLLAMA_HOST=127.0.0.1:11434 ollama serve",
            lambda: ollama.reachable(ollama_host_url),
        ),
        (
            True,
            "an Ollama model pulled",
            "pull a tool-capable model: ollama pull gemma4:26b",
            lambda: _cmd_ok("ollama", "list") and _ollama_has_model(),
        ),
    ]
    for required, label, fix, pred in checks:
        _report(tally, required=required, label=label, fix=fix, ok=_safe(pred))

    # Public-interface bind is a WARN, not a hard failure: a 0.0.0.0 Ollama works but
    # exposes it to the LAN. The sandbox reaches a loopback-only server through its
    # host proxy, so loopback-only is both reachable AND the safer binding.
    exposure = ollama.lan_exposure_warning()
    _report(
        tally,
        required=False,
        label="Ollama bound loopback-only (not exposed to the LAN)",
        fix=(exposure or "OLLAMA_HOST=127.0.0.1:11434 ollama serve"),
        ok=exposure is None,
    )

    console.print()
    if tally.failed:
        console.print(
            f"[red]{tally.failed} required check(s) failed[/red]; {tally.warned} warning(s)."
        )
    else:
        console.print(f"[green]All required checks passed[/green] ({tally.warned} warning(s)).")
    return tally.failed


def _ollama_has_model() -> bool:
    """True if `ollama list` shows at least one model row."""
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, check=False).stdout
    except (FileNotFoundError, OSError):
        return False
    rows = [ln for ln in out.splitlines()[1:] if ln.strip()]
    return bool(rows)


def _safe(pred: Callable[[], bool]) -> bool:
    try:
        return pred()
    except Exception:
        return False
