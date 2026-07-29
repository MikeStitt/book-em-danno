"""Run-level provenance for `danno bench` (§7) — always written, one-shot at run
start, to `provenance.json`. Cheap, host-side metadata that makes a bench result
reproducible and comparable: exact model bytes (digest) and static model facts
(quant, param count, context length), the harness/fork pins that produced the run, the
danno version + commit, and a host descriptor (CPU/RAM/GPU). A separate file keeps
`bench.json`'s schema stable/backward-compatible.

Everything is best-effort: a probe that can't reach Ollama, a mac dev host with no
`/proc`, or an absent `nvidia-smi` yields `null` fields, never a failure.
"""

from __future__ import annotations

import json
import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

from book_em_danno.commands import ollama, sandbox_cli
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import Runner
from danno_validator import harnesses
from danno_validator.matrix import ConfigVariant
from danno_validator.suites.config import GatesConfig
from danno_validator.telemetry.sampler import read_memory

_OLLAMA_PREFIX = "ollama/"

# A harness ridden on the generic "shell" image is danno-installed at a pinned version
# (claurst/codex — recorded via `_provenance`); anything else ships its binary in a prebuilt
# `docker sandbox create <image>` image (opencode/claude), so danno pins no version and must
# ask the live VM for it (`probe_harness_version`).
_SHELL_IMAGE = "shell"


def danno_version() -> dict:
    """danno's package version + short git commit (best-effort; `null` when either is
    unavailable, e.g. an installed wheel outside a git checkout)."""
    try:
        ver: str | None = pkg_version("danno")
    except PackageNotFoundError:
        ver = None
    return {"version": ver, "commit": _git_commit()}


def _git_commit() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=False
        )
    except (FileNotFoundError, OSError):
        return None
    out = proc.stdout.strip()
    return out or None


def _is_image_provided(harness: str) -> bool:
    """True for a harness whose binary ships in its prebuilt sandbox image (opencode/claude)
    rather than being danno-installed at a pinned version (claurst/codex ride the generic
    "shell" image). Only the former needs a live `--version` probe — the latter's version is
    already a danno-owned constant in `_provenance` (#89 F5-B)."""
    return harnesses.get(harness).sandbox_image != _SHELL_IMAGE


def probe_harness_version(runner: Runner, sandbox: str, harness: str) -> str | None:
    """Best-effort `<harness> --version` in the live sandbox VM, for an image-provided harness
    whose binary version danno does not pin (opencode/claude — #89 F5-B).

    Returns the trimmed first non-blank line of output, or None for a danno-installed harness
    (its version is already a pinned constant, so no probe) or when the probe cannot run (the
    sandbox is gone, the binary lacks `--version`, a non-zero exit). Never raises — a missing
    version is a `null` field, never a run failure. opencode's version is the V5 canary: it
    decides whether `agent.steps` is honored."""
    if not _is_image_provided(harness):
        return None
    try:
        result = runner.capture([*sandbox_cli.base(), "exec", sandbox, harness, "--version"])
    except OSError:
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.strip():
            return line.strip()
    return None


def harness_provenance(
    harness: str, config: DannoConfig, *, probed_version: str | None = None
) -> dict:
    """The danno-known version pins for the harness that ran (§7.3). opencode/claude are
    image-provided (the prebuilt sandbox ships the binary), so danno pins no version —
    only claurst/codex (release/npm tag) are danno-owned. Each harness owns its pins via
    `Harness.provenance`; this merges them under the `harness` label. `probed_version`, when
    given (the live-VM `<harness> --version` from `probe_harness_version`), adds a `version`
    field for the image-provided harnesses that otherwise record only their name (#89 F5-B)."""
    info = {"harness": harness, **harnesses.get(harness).provenance(config)}
    if probed_version is not None:
        info["version"] = probed_version
    return info


def _read_cpuinfo() -> dict:
    """CPU model + logical core count from `/proc/cpuinfo` ({} off Linux)."""
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8")
    except OSError:
        return {}
    model: str | None = None
    cores = 0
    for line in text.splitlines():
        key, _, val = line.partition(":")
        key = key.strip()
        if key == "model name" and model is None:
            model = val.strip()
        elif key == "processor":
            cores += 1  # one line per logical CPU
    out: dict = {}
    if model:
        out["cpu_model"] = model
    if cores:
        out["cpu_cores"] = cores
    return out


_GPU_QUERY = "name,driver_version,memory.total"


def _read_gpu_descriptor() -> list[dict]:
    """Per-GPU `{name, driver, vram_total_mb}` via `nvidia-smi` ([] if absent)."""
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={_GPU_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    if proc.returncode != 0:
        return []
    return _parse_gpu_descriptor(proc.stdout)


def _parse_gpu_descriptor(text: str) -> list[dict]:
    gpus: list[dict] = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != 3:
            continue
        try:
            vram = float(cells[2])
        except ValueError:
            vram = 0.0
        gpus.append({"name": cells[0], "driver": cells[1], "vram_total_mb": vram})
    return gpus


def host_descriptor() -> dict:
    """A one-shot CPU/RAM/GPU descriptor of the bench host (§7.4). Null/empty fields
    off the Linux/NVIDIA host — nothing Apple-specific."""
    out = _read_cpuinfo()
    memory = read_memory()
    if memory:
        out["mem_total_kb"] = memory["total_kb"]
    gpus = _read_gpu_descriptor()
    if gpus:
        out["gpus"] = gpus
    return out


def model_provenance(model_ref: str, host_url: str = ollama.DEFAULT_HOST_URL) -> dict:
    """Digest (§7.1) + static facts (§7.2) for one model ref. Only local `ollama/<tag>`
    refs are probed (cloud refs have no local Ollama to ask); returns `{}` for those."""
    if not model_ref.startswith(_OLLAMA_PREFIX):
        return {}
    tag = model_ref[len(_OLLAMA_PREFIX) :]
    facts = dict(ollama.model_params(tag, host_url))
    digest = ollama.model_digest(tag, host_url)
    if digest:
        facts["digest"] = digest
    return facts


def collect_provenance(
    config: DannoConfig,
    variants: list[ConfigVariant],
    *,
    harness: str,
    sample_interval_s: float | None,
    warmup: list[dict] | None = None,
    host_url: str = ollama.DEFAULT_HOST_URL,
    gates: GatesConfig | None = None,
    harness_version: str | None = None,
) -> dict:
    """Assemble the full provenance payload for a bench run (always written).

    `warmup` is the per-tag pre-warm record (`ollama.warm_model`) — empty when `--no-warm`
    or no local models — so a reader can tell whether the timed cells started from a warm
    model or paid a cold load. `gates` records the resolved runaway-gate config so a
    cross-run comparison knows what caps were in force (`.docs/plan-bench-runaway-gates.md`).
    `harness_version` is the live-VM `<harness> --version` probed while a sandbox was up, for
    image-provided harnesses danno does not pin (opencode/claude — #89 F5-B)."""
    return {
        "danno": danno_version(),
        "host": host_descriptor(),
        "harness_versions": harness_provenance(harness, config, probed_version=harness_version),
        "sample_interval_s": sample_interval_s,
        "warmup": warmup or [],
        "gates": gates.model_dump() if gates is not None else None,
        "models": {v.model_ref: model_provenance(v.model_ref, host_url) for v in variants},
    }


def write_provenance(out_dir: Path, provenance: dict) -> Path:
    """Write `provenance.json` under `out_dir` and return its path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "provenance.json"
    path.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    return path
