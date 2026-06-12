"""Tool-catalog installation (port of `scripts/install-ados.sh` + generic).

Each `[[tools]]` entry in danno.toml installs per `install_to`. ADOS is the
special case (its own installer only writes the global ~/.config/opencode, which
the sandbox can't see, so we also copy the agent/command defs project-local and
record provenance). Other git-sourced tools get a generic clone+install advisory.
A tool whose `source` is not a git repo and isn't otherwise recognized (e.g. the
hosted plannotator) is skipped with a warning that its install path is unconfirmed
— rather than running a guessed-at installer (Working Rule 8: never pretend).
"""

from __future__ import annotations

import filecmp
import os
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ..config.schema import Tool
from ..core.exec import Runner, log_info, log_warn


class ToolInstallError(Exception):
    """A tool cannot be installed as configured (fail loud)."""


def resolve_ados_repo(ados_repo: str | None = None) -> Path:
    """Locate an ADOS checkout containing .opencode/agent.

    Order: explicit arg, ADOS_REPO env, ~/.ados/repo, sibling ../agentic-delivery-os.
    """
    candidates: list[Path] = []
    if ados_repo:
        candidates.append(Path(ados_repo))
    elif env := os.environ.get("ADOS_REPO"):
        candidates.append(Path(env))
    candidates += [Path.home() / ".ados" / "repo", Path.cwd().parent / "agentic-delivery-os"]
    for c in candidates:
        if (c / ".opencode" / "agent").is_dir():
            return c.resolve()
    raise ToolInstallError(
        "cannot find an ADOS checkout (need one with .opencode/agent). "
        "Pass --ados-repo <dir> or set ADOS_REPO."
    )


def _copy_md_dir(runner: Runner, src: Path, dest: Path, label: str) -> None:
    """Copy *.md from src to dest, only when missing or changed (idempotent)."""
    if not src.is_dir():
        log_warn(f"missing {label} source: {src}")
        return
    md_files = sorted(src.glob("*.md"))
    if runner.apply and not runner.dry_run:
        dest.mkdir(parents=True, exist_ok=True)
        for f in md_files:
            target = dest / f.name
            if target.is_file() and filecmp.cmp(f, target, shallow=False):
                continue
            shutil.copy2(f, target)
            log_info(f"copy {label}/{f.name}")
    else:
        runner.advise(
            ["cp", f"{src}/*.md", str(dest) + "/"],
            why=f"copy ADOS {label} definitions project-local ({len(md_files)} file(s))",
        )


def _write_provenance(ados: Path, target_abs: Path) -> None:
    sha = "unknown"
    try:
        sha = (
            subprocess.run(
                ["git", "-C", str(ados), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            or "unknown"
        )
    except (FileNotFoundError, OSError):
        pass
    out = target_abs / ".opencode" / "ados-provenance.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    when = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    out.write_text(
        f"ADOS source:  {ados}\nADOS commit:  {sha}\nInstalled:    {when}\n"
        f"Installed by: book-em-danno (danno install)\n",
        encoding="utf-8",
    )
    log_info(f"wrote {out} (ADOS {sha})")


def install_ados(
    runner: Runner, tool: Tool, target_abs: Path, *, ados_repo: str | None = None
) -> None:
    """Run ADOS's --local installer in the project, copy agent/command defs
    project-local (the sandbox can't see ~/.config/opencode), record provenance."""
    ados = resolve_ados_repo(ados_repo)
    installer = ados / "scripts" / "install.sh"
    if not installer.is_file():
        raise ToolInstallError(f"ADOS installer not found: {installer}")
    log_info(f"ADOS source: {ados}")
    runner.advise(
        ["bash", str(installer), "--local", "--no-fetch"],
        why=f"run ADOS's local install in {target_abs} (cwd=target, ADOS_SOURCE_DIR={ados})",
        cwd=target_abs,
        env={**os.environ, "ADOS_SOURCE_DIR": str(ados)},
    )
    _copy_md_dir(runner, ados / ".opencode" / "agent", target_abs / ".opencode" / "agent", "agent")
    _copy_md_dir(
        runner, ados / ".opencode" / "command", target_abs / ".opencode" / "command", "command"
    )
    if runner.apply and not runner.dry_run:
        _write_provenance(ados, target_abs)
    else:
        log_info(f"would record ADOS provenance in {target_abs}/.opencode/ados-provenance.txt")


def install_generic_git(runner: Runner, tool: Tool, target_abs: Path) -> None:
    """Clone a git-sourced tool into a temp dir and run its installer (advisory).

    Clones into a fresh temp dir, never the CWD: under --apply a bare
    `git clone <source>` would pollute the repo root. (No in-scope tool uses this
    path now — OpenCode plugins go through `[[npm]]`, not here — but it stays a
    fixed, non-footgun fallback for a future non-plugin git tool.)
    """
    dest = Path(tempfile.mkdtemp(prefix="danno-tool-")) / tool.name
    runner.advise(
        ["git", "clone", tool.source, str(dest)],
        why=f"clone tool '{tool.name}' from {tool.source} into a temp dir",
    )
    log_info(
        f"after clone, run {tool.name}'s installer per its README; for install_to="
        f"'{tool.install_to}' it lands in the {tool.install_to}."
    )


def _is_git_source(source: str) -> bool:
    return source.endswith(".git") or "github.com" in source or "gitlab.com" in source


def install_tool(
    runner: Runner, tool: Tool, target_abs: Path, *, ados_repo: str | None = None
) -> None:
    """Dispatch a single catalog tool to its installer."""
    if tool.name == "ados":
        install_ados(runner, tool, target_abs, ados_repo=ados_repo)
    elif _is_git_source(tool.source):
        install_generic_git(runner, tool, target_abs)
    else:
        # Hosted/web tool with no known local install mechanism — don't fabricate one.
        log_warn(
            f"tool '{tool.name}' ({tool.source}) has no known local install mechanism. "
            "TODO: confirm how it installs; skipping for now."
        )
