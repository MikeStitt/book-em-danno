"""Host-side resource sampler for `danno bench --sample` (§5, §2.5).

A background thread that, while an agent turn runs, appends a JSON line per tick to
`samples/<suite>/<task>/<slug>.jsonl` — CPU/memory from Linux `/proc`, GPU from
`nvidia-smi`, and model-attributed VRAM from Ollama `/api/ps`. Every backend is
best-effort: a missing `/proc` (mac dev host) or absent `nvidia-smi` degrades that
field to `None`/`[]` and never fails the bench. Stdlib only — no new dependency.

`summarize` reduces a turn's sample file to peak/mean rollups (§5.5) and a best-effort
model-load time (§2.5); `SampleBinding` mints the per-permutation sampler, mirroring
`capture.wiring.CaptureBinding` so `danno bench` composes both the same way.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from book_em_danno.capture.wiring import perm_segment
from book_em_danno.commands import ollama

# --- stdlib sampling backends (each returns None/[] on any failure) -----------


def read_cpu_times() -> tuple[int, int] | None:
    """`(idle, total)` jiffies from the aggregate `cpu` line of `/proc/stat`, or None
    (no `/proc` — e.g. the mac dev host). Utilization is a delta between two reads."""
    try:
        with open("/proc/stat", encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return None
    parts = first.split()
    if not parts or parts[0] != "cpu" or len(parts) < 5:
        return None
    try:
        fields = [int(x) for x in parts[1:]]
    except ValueError:
        return None
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
    return idle, sum(fields)


def _cpu_util(prev: tuple[int, int] | None, cur: tuple[int, int] | None) -> float | None:
    """Utilization % from two `/proc/stat` reads; None on the first tick or no delta."""
    if prev is None or cur is None:
        return None
    idle_delta = cur[0] - prev[0]
    total_delta = cur[1] - prev[1]
    if total_delta <= 0:
        return None
    return round(100.0 * (1.0 - idle_delta / total_delta), 1)


def read_memory() -> dict[str, int] | None:
    """`{used_kb, total_kb, available_kb}` from `/proc/meminfo`, or None."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            fields = {}
            for line in fh:
                key, _, rest = line.partition(":")
                fields[key.strip()] = int(rest.split()[0])  # value is in kB
    except (OSError, ValueError, IndexError):
        return None
    total = fields.get("MemTotal")
    avail = fields.get("MemAvailable")
    if total is None or avail is None:
        return None
    return {"used_kb": total - avail, "total_kb": total, "available_kb": avail}


_NVIDIA_QUERY = "utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"


def read_gpus() -> list[dict[str, float]]:
    """Per-GPU `{util, mem_used_mb, mem_total_mb, temp_c, power_w}` via `nvidia-smi`.

    Returns `[]` when `nvidia-smi` is absent (mac / non-NVIDIA host) or errors —
    GPU rows are simply omitted, never a failure."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                f"--query-gpu={_NVIDIA_QUERY}",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []
    if proc.returncode != 0:
        return []
    return parse_nvidia_smi(proc.stdout)


def parse_nvidia_smi(text: str) -> list[dict[str, float]]:
    """Parse `nvidia-smi --format=csv,noheader,nounits` rows (pure, for tests)."""
    keys = ("util", "mem_used_mb", "mem_total_mb", "temp_c", "power_w")
    gpus: list[dict[str, float]] = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != len(keys):
            continue
        row: dict[str, float] = {}
        for key, cell in zip(keys, cells, strict=True):
            try:
                row[key] = float(cell)
            except ValueError:
                row[key] = 0.0  # e.g. "[N/A]" from a MIG / unsupported field
        gpus.append(row)
    return gpus


# --- the sampler --------------------------------------------------------------


@dataclass
class ResourceSampler:
    """Appends a resource sample to `out_path` every `interval` seconds on a daemon
    thread until `.stop()`. Use `.running()` (or `SampleBinding.permutation`) to
    bracket a turn. CPU utilization needs two reads, so the first tick reports
    `cpu=None`."""

    out_path: Path
    interval: float
    host_url: str = ollama.DEFAULT_HOST_URL
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _prev_cpu: tuple[int, int] | None = field(default=None, init=False, repr=False)
    _elapsed: float = field(default=0.0, init=False, repr=False)

    def start(self) -> None:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.out_path.write_text("", encoding="utf-8")
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @contextmanager
    def running(self) -> Iterator[ResourceSampler]:
        self.start()
        try:
            yield self
        finally:
            self.stop()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._tick()
            self._elapsed += self.interval
            self._stop.wait(self.interval)

    def _tick(self) -> None:
        cur_cpu = read_cpu_times()
        sample = {
            "t": round(self._elapsed, 3),
            "cpu": _cpu_util(self._prev_cpu, cur_cpu),
            "mem": read_memory(),
            "gpu": read_gpus(),
            "model_ps": _running_models_safe(self.host_url),
        }
        self._prev_cpu = cur_cpu
        with contextlib.suppress(OSError):
            with open(self.out_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(sample) + "\n")


def _running_models_safe(host_url: str) -> list[dict]:
    """`ollama.running_models` is already best-effort; guard the sampler regardless."""
    try:
        return ollama.running_models(host_url)
    except Exception:  # never let a probe kill the sampler thread
        return []


# --- reduction (§5.5, §2.5) ---------------------------------------------------


@dataclass(frozen=True)
class ResourceSummary:
    """Peak/mean rollups for one turn (§5.5) plus best-effort model-load time (§2.5).

    Every field is optional: a field is `None` when its backend produced no samples
    (e.g. `gpu_util_peak` off a non-NVIDIA host). `sample_count` records how many
    ticks landed, so a zero-sample turn is distinguishable from an all-idle one."""

    sample_count: int
    cpu_peak: float | None = None
    cpu_mean: float | None = None
    mem_peak_kb: int | None = None
    gpu_util_peak: float | None = None
    gpu_util_mean: float | None = None
    vram_peak_mb: float | None = None
    model_vram_peak_bytes: int | None = None
    model_load_s: float | None = None


def _peak(values: list[float]) -> float | None:
    return round(max(values), 1) if values else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def read_samples(path: Path) -> list[dict]:
    """Parse a sample file into its list of tick dicts ([] if absent)."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def summarize(samples: list[dict]) -> ResourceSummary:
    """Reduce a turn's ticks to peak/mean rollups (§5.5) + model-load time (§2.5)."""
    cpu = [s["cpu"] for s in samples if s.get("cpu") is not None]
    mem = [s["mem"]["used_kb"] for s in samples if s.get("mem")]
    gpu_util: list[float] = []
    vram_mb: list[float] = []
    for s in samples:
        for g in s.get("gpu") or []:
            if "util" in g:
                gpu_util.append(g["util"])
            if "mem_used_mb" in g:
                vram_mb.append(g["mem_used_mb"])
    model_vram = [
        m["size_vram"]
        for s in samples
        for m in (s.get("model_ps") or [])
        if isinstance(m.get("size_vram"), int)
    ]
    return ResourceSummary(
        sample_count=len(samples),
        cpu_peak=_peak(cpu),
        cpu_mean=_mean(cpu),
        mem_peak_kb=max(mem) if mem else None,
        gpu_util_peak=_peak(gpu_util),
        gpu_util_mean=_mean(gpu_util),
        vram_peak_mb=_peak(vram_mb),
        model_vram_peak_bytes=max(model_vram) if model_vram else None,
        model_load_s=_model_load_s(samples),
    )


def _model_load_s(samples: list[dict]) -> float | None:
    """Best-effort model-load time (§2.5): the `t` of the first tick at which a model
    is resident in `/api/ps`, relative to turn start. `None` if a model was already
    loaded on tick 0 (can't attribute a load to this turn) or never observed."""
    first_resident: float | None = None
    for s in samples:
        if s.get("model_ps"):
            first_resident = s.get("t")
            break
    if first_resident is None or first_resident == 0:
        return None
    return round(first_resident, 3)


# --- per-permutation binding (mirrors capture.wiring.CaptureBinding) -----------


@dataclass(frozen=True)
class SampleBinding:
    """The sample root dir + interval, ready to mint a per-permutation sampler for
    `danno bench --sample`. `permutation` returns a running-sampler context whose
    file is namespaced `<sample_dir>/<suite>/<task>/<slug>.jsonl`."""

    sample_dir: Path
    interval: float
    host_url: str = ollama.DEFAULT_HOST_URL

    def permutation_path(self, *, suite: str, task_id: str, model: str | None) -> Path:
        return self.sample_dir / perm_segment(suite, task_id, model).with_suffix(".jsonl")

    def permutation(
        self, *, suite: str, task_id: str, model: str | None
    ) -> AbstractContextManager[ResourceSampler]:
        path = self.permutation_path(suite=suite, task_id=task_id, model=model)
        sampler = ResourceSampler(out_path=path, interval=self.interval, host_url=self.host_url)
        return sampler.running()


def summary_to_dict(summary: ResourceSummary) -> dict:
    """The summary as a plain dict for `bench.json`/`provenance.json` embedding."""
    return asdict(summary)
