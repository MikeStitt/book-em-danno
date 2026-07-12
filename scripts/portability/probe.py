#!/usr/bin/env python3
"""danno cross-platform portability probe (Tier 1, safe surfaces).

Runs danno's *side-effect-free* surfaces in the CURRENT shell/OS, records
environment facts (including Git Bash presence -> H1 honesty), preflights the
run-leg prerequisites (docker + docker-sandbox + a reachable Ollama), and writes
a JSON + Markdown report you can commit for central synthesis.

Characterization only: it never runs `install --apply`, never starts a bench,
and its only side effect is writing report files under `--out`. Std-lib only, so
it runs anywhere danno (or `uv`) is on PATH.

See `.docs/plan-test-danno-cross-platform.md` (Tier 1, hazards H1-H10).

Usage (identical across cmd / PowerShell / bash / WSL):
    uv run python scripts/portability/probe.py --shell cmd \
        --ollama-host http://10.0.1.27:11434
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

TRUNC = 2000  # max chars of captured stdout/stderr kept per command


def truncate(text: str, limit: int = TRUNC) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + "\n... [truncated]"


def detect_shell(override: str | None) -> tuple[str, dict[str, str]]:
    """Best-effort shell label + raw hints; `--shell` wins (the honest label)."""
    hints: dict[str, str] = {}
    wsl = os.environ.get("WSL_DISTRO_NAME")
    if wsl:
        hints["WSL_DISTRO_NAME"] = wsl
    if os.environ.get("PSModulePath"):
        hints["PSModulePath"] = "present"
    if os.environ.get("COMSPEC"):
        hints["COMSPEC"] = os.environ["COMSPEC"]
    if override:
        return override, hints
    if wsl:
        return "wsl", hints
    if sys.platform == "win32":
        return "windows", hints
    if sys.platform == "darwin":
        return "macos", hints
    return "linux", hints


def resolve_danno() -> list[str] | None:
    """How to invoke danno here: the console script, else `uv run danno`."""
    if shutil.which("danno"):
        return ["danno"]
    if shutil.which("uv"):
        return ["uv", "run", "danno"]
    return None


def run_cmd(cmd: list[str], timeout: int) -> dict[str, object]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return {"cmd": cmd, "ok": False, "returncode": None, "error": f"not found: {exc}"}
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "ok": False, "returncode": None, "error": f"timeout after {timeout}s"}
    except OSError as exc:
        return {"cmd": cmd, "ok": False, "returncode": None, "error": str(exc)}
    return {
        "cmd": cmd,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": truncate(proc.stdout),
        "stderr": truncate(proc.stderr),
        "error": None,
    }


def check_ollama(base_url: str, timeout: int = 10) -> dict[str, object]:
    url = base_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return {"url": url, "reachable": False, "error": str(exc), "models": []}
    models = [m.get("name", "") for m in payload.get("models", [])]
    return {"url": url, "reachable": True, "error": None, "models": models}


def classify_bash() -> tuple[str | None, str]:
    """Resolve `bash` on PATH and classify it: git-bash | wsl-shim | other | absent.

    On Windows the distinction is load-bearing: Git Bash actually runs POSIX
    scripts, but `C:\\Windows\\System32\\bash.exe` is the WSL launcher -> danno's
    host `bash` subprocess (tools.py:107) would run INSIDE the WSL distro.
    """
    path = shutil.which("bash")
    if not path:
        return None, "absent"
    low = path.lower()
    if "system32" in low:
        return path, "wsl-shim"
    if "\\git\\" in low or "/git/" in low:
        return path, "git-bash"
    return path, "other"


def collect_env(shell_label: str, hints: dict[str, str]) -> dict[str, object]:
    tools = ("danno", "uv", "docker", "sbx", "git", "bash", "ollama")
    bash_path, bash_kind = classify_bash()
    return {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "shell_label": shell_label,
        "shell_hints": hints,
        "platform": sys.platform,
        "os": platform.system(),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "cwd": str(Path.cwd()),
        "which": {t: shutil.which(t) for t in tools},
        "bash_path": bash_path,
        "bash_kind": bash_kind,
        "danno_toml_in_cwd": Path("danno.toml").is_file(),
    }


def probe_surfaces(danno: list[str], has_toml: bool, timeout: int) -> list[dict[str, object]]:
    """danno's safe surfaces (S0-S2). None mutate a target repo."""
    plan: list[tuple[str, list[str]]] = [
        ("S0 danno --help", [*danno, "--help"]),
        ("S0 danno doctor --help", [*danno, "doctor", "--help"]),
        ("S1 danno doctor", [*danno, "doctor"]),
        ("S2 danno install --help", [*danno, "install", "--help"]),
    ]
    if has_toml:
        # Only meaningful with a danno.toml present; advise-only, no side effects.
        plan.append(("S2 danno install --dry-run", [*danno, "install", "--dry-run"]))
    results: list[dict[str, object]] = []
    for label, cmd in plan:
        result = run_cmd(cmd, timeout)
        result["surface"] = label
        results.append(result)
    return results


def preflight(ollama_host: str, timeout: int) -> dict[str, object]:
    """Run-leg prerequisites: the sandbox CLI (`sbx` or legacy `docker sandbox`),
    docker (info only — `sbx` is standalone), and Ollama reachability."""
    docker = run_cmd(["docker", "version", "--format", "{{.Server.Version}}"], timeout)
    sbx = run_cmd(["sbx", "version"], timeout)
    legacy = run_cmd(["docker", "sandbox", "--help"], timeout)
    ollama = check_ollama(ollama_host)
    sandbox_cli = "sbx" if sbx["ok"] else ("docker sandbox" if legacy["ok"] else None)
    ready = bool(sandbox_cli) and bool(ollama["reachable"])
    return {
        "docker": docker,
        "sbx": sbx,
        "docker_sandbox_legacy": legacy,
        "sandbox_cli": sandbox_cli,
        "ollama": ollama,
        "run_leg_ready": ready,
    }


def capture_sbx(timeout: int) -> dict[str, object] | None:
    """Capture `sbx` help surfaces for the migration (investigations I1-I4/I6).

    None when `sbx` is absent. Full outputs land in the JSON so the sbx->danno
    command mapping (exec `--env-file`/`-it`, `policy allow` syntax, `create`
    blueprints, `secret` model) can be resolved from the real CLI.
    """
    if not shutil.which("sbx"):
        return None
    probes = {
        "version": ["sbx", "version"],
        "create --help": ["sbx", "create", "--help"],
        "exec --help": ["sbx", "exec", "--help"],
        "policy --help": ["sbx", "policy", "--help"],
        "secret --help": ["sbx", "secret", "--help"],
    }
    return {key: run_cmd(cmd, timeout) for key, cmd in probes.items()}


def hazard_notes(env: dict[str, object]) -> list[str]:
    notes: list[str] = []
    if env["platform"] == "win32":
        kind = env["bash_kind"]
        path = env["bash_path"]
        if kind == "git-bash":
            notes.append(
                f"H1: `bash` -> Git Bash ({path}). danno's host bash subprocess (tools.py:107) "
                "is FOUND (no OS not-found error), but Git-Bash path translation / docker interop "
                "under it is UNVERIFIED — not a clean pass, verify the run leg."
            )
        elif kind == "wsl-shim":
            notes.append(
                f"H1 WARNING: `bash` -> the WSL shim ({path}), NOT Git Bash. danno's host bash "
                "subprocess (tools.py:107) would run the installer INSIDE your WSL distro against "
                "/mnt/c paths (cross-environment) — likely misbehaves."
            )
        elif kind == "other":
            notes.append(
                f"H1: `bash` -> {path} (unclassified). Verify it can run danno's host installer "
                "script (tools.py:107)."
            )
        else:  # absent
            notes.append(
                "H1 will FIRE: no `bash` on PATH -> danno's host bash subprocess "
                "(tools.py:107, `install --apply`) fails at the OS level."
            )
        notes.append(
            "H4: os.chmod(0o600) is a no-op on native Windows -> secret env-files are NOT "
            "owner-restricted. Do NOT run cloud-auth benches here until a Windows-ACL path exists."
        )
    return notes


def slugify(env: dict[str, object]) -> str:
    pv = f"py{sys.version_info.major}{sys.version_info.minor}"
    raw = f"report-{env['os']}-{env['shell_label']}-{pv}"
    return "".join(c if (c.isalnum() or c in "-._") else "-" for c in raw).lower()


def render_markdown(report: dict[str, object]) -> str:
    env = report["env"]
    lines: list[str] = []
    lines.append(f"# danno portability probe — {env['os']} / {env['shell_label']}")
    lines.append("")
    lines.append(f"- **when (UTC):** {env['timestamp_utc']}")
    lines.append(f"- **platform / arch:** `{env['platform']}` / `{env['arch']}`  ")
    lines.append(f"- **os:** {env['os']} {env['os_release']}")
    lines.append(f"- **python:** {env['python']} (`{env['python_exe']}`)")
    lines.append(f"- **bash:** {env['bash_kind']} (`{env['bash_path'] or 'absent'}`)")
    lines.append(f"- **cwd:** `{env['cwd']}`")
    lines.append(f"- **danno.toml in cwd:** {env['danno_toml_in_cwd']}")
    lines.append("")
    lines.append("## Tooling on PATH")
    lines.append("")
    lines.append("| tool | path |")
    lines.append("|---|---|")
    for tool, path in env["which"].items():
        lines.append(f"| {tool} | {path or '**MISSING**'} |")
    lines.append("")
    lines.append("## Surfaces (S0-S2)")
    lines.append("")
    lines.append("| surface | ok | exit | note |")
    lines.append("|---|---|---|---|")
    for surf in report["surfaces"]:
        note = surf.get("error") or ((surf.get("stderr") or "").splitlines()[:1] or [""])[0]
        lines.append(
            f"| {surf['surface']} | {'✅' if surf['ok'] else '❌'} | "
            f"{surf['returncode']} | {note[:80]} |"
        )
    lines.append("")
    pf = report["preflight"]
    dk, oll = pf["docker"], pf["ollama"]
    sbx_v, legacy = pf["sbx"], pf["docker_sandbox_legacy"]
    oll_status = "✅ reachable" if oll["reachable"] else f"❌ {oll['error']}"
    lines.append("## Run-leg preflight (Tier 1, P3+)")
    lines.append("")
    lines.append(f"- **active sandbox CLI:** {pf['sandbox_cli'] or '**none**'} — R1/I5 signal")
    lines.append(f"- **sbx:** {'✅' if sbx_v['ok'] else '❌'} (exit {sbx_v['returncode']})")
    lines.append(
        f"- **docker sandbox (legacy, deprecated):** {'✅' if legacy['ok'] else '❌'} "
        f"(exit {legacy['returncode']})"
    )
    lines.append(f"- **docker daemon:** {'✅' if dk['ok'] else '❌'} (exit {dk['returncode']})")
    lines.append(f"- **ollama {oll['url']}:** {oll_status}")
    if oll["reachable"]:
        lines.append(f"  - models: {', '.join(oll['models']) or '(none pulled)'}")
    lines.append(f"- **run-leg ready:** {'✅ yes' if pf['run_leg_ready'] else '❌ no'}")
    lines.append("")
    sbx_cap = report.get("sbx")
    if sbx_cap:
        lines.append("## sbx help capture (migration I1-I4)")
        lines.append("")
        for key, res in sbx_cap.items():
            lines.append(f"### `sbx {key}`")
            lines.append("")
            lines.append("```")
            lines.append((res.get("stdout") or res.get("error") or "").strip() or "(no output)")
            lines.append("```")
            lines.append("")
    if report["hazards"]:
        lines.append("## Hazard notes")
        lines.append("")
        for note in report["hazards"]:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="danno cross-platform portability probe")
    parser.add_argument("--shell", default=None, help="run label: cmd|powershell|wsl|ubuntu|…")
    parser.add_argument(
        "--ollama-host",
        default=os.environ.get("DANNO_PROBE_OLLAMA", "http://localhost:11434"),
        help="Ollama base URL for the reachability check (env DANNO_PROBE_OLLAMA)",
    )
    parser.add_argument("--out", default="scripts/portability/reports", help="report output dir")
    parser.add_argument("--timeout", type=int, default=120, help="per-command timeout (s)")
    args = parser.parse_args()

    shell_label, hints = detect_shell(args.shell)
    env = collect_env(shell_label, hints)

    danno = resolve_danno()
    if danno is None:
        print("FATAL: neither `danno` nor `uv` found on PATH — cannot probe danno surfaces.")
        print("Install danno (uv sync) or put `uv` on PATH, then re-run.")
        env["fatal"] = "no danno/uv on PATH"
        surfaces: list[dict[str, object]] = []
    else:
        surfaces = probe_surfaces(danno, bool(env["danno_toml_in_cwd"]), args.timeout)

    report: dict[str, object] = {
        "env": env,
        "danno_invocation": danno,
        "surfaces": surfaces,
        "preflight": preflight(args.ollama_host, args.timeout),
        "sbx": capture_sbx(args.timeout),
        "hazards": hazard_notes(env),
    }

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(env)
    (out_dir / f"{slug}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (out_dir / f"{slug}.md").write_text(render_markdown(report), encoding="utf-8")

    passed = sum(1 for s in surfaces if s["ok"])
    pf = report["preflight"]
    print(
        f"[probe] {env['os']} / {shell_label} — surfaces {passed}/{len(surfaces)} ok; "
        f"sandbox CLI: {pf['sandbox_cli']}; run-leg ready: {pf['run_leg_ready']}"
    )
    for note in report["hazards"]:
        print(f"[probe] hazard: {note.splitlines()[0]}")
    print(f"[probe] wrote {out_dir / slug}.json and .md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
