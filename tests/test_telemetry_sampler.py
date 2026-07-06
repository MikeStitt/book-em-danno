"""Unit tests for the bench resource sampler (`telemetry.sampler`). Pure parsers and
the reduce/binding logic; the sampling thread is exercised with stubbed backends so
the tests stay hermetic (no real `/proc`, `nvidia-smi`, or Ollama)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from danno_validator.telemetry import sampler


def test_parse_nvidia_smi_parses_rows() -> None:
    out = "45, 8192, 24576, 61, 210.5\n72, 12000, 24576, 65, 250.0\n"
    gpus = sampler.parse_nvidia_smi(out)
    assert gpus == [
        {
            "util": 45.0,
            "mem_used_mb": 8192.0,
            "mem_total_mb": 24576.0,
            "temp_c": 61.0,
            "power_w": 210.5,
        },
        {
            "util": 72.0,
            "mem_used_mb": 12000.0,
            "mem_total_mb": 24576.0,
            "temp_c": 65.0,
            "power_w": 250.0,
        },
    ]


def test_parse_nvidia_smi_tolerates_na_and_junk() -> None:
    # A MIG/unsupported field reports "[N/A]"; a short/blank line is skipped.
    gpus = sampler.parse_nvidia_smi("30, 100, 200, [N/A], 50\n\ngarbage line\n")
    assert len(gpus) == 1
    assert gpus[0]["temp_c"] == 0.0  # unparseable field → 0.0, row kept


def test_cpu_util_delta() -> None:
    # idle jumps 850→1700 (Δ850), total 1000→2000 (Δ1000) → 15% busy.
    assert sampler._cpu_util((850, 1000), (1700, 2000)) == 15.0
    assert sampler._cpu_util(None, (1, 2)) is None  # first tick has no prev
    assert sampler._cpu_util((1, 2), (1, 2)) is None  # no time elapsed


def test_read_cpu_times_and_memory_from_proc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stat = tmp_path / "stat"
    stat.write_text("cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 ...\n", encoding="utf-8")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       32000 kB\nMemFree: 1000 kB\nMemAvailable:   20000 kB\n")
    real_open = open

    def fake_open(path, *a, **k):  # type: ignore[no-untyped-def]
        mapped = {"/proc/stat": stat, "/proc/meminfo": meminfo}.get(path, path)
        return real_open(mapped, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)
    assert sampler.read_cpu_times() == (850, 1000)  # idle(800)+iowait(50), sum
    assert sampler.read_memory() == {"used_kb": 12000, "total_kb": 32000, "available_kb": 20000}


def test_summarize_rolls_up_peaks_and_means() -> None:
    samples = [
        {
            "t": 0.0,
            "cpu": None,
            "mem": {"used_kb": 100},
            "gpu": [{"util": 10.0, "mem_used_mb": 500.0}],
            "model_ps": [],
        },
        {
            "t": 0.5,
            "cpu": 40.0,
            "mem": {"used_kb": 300},
            "gpu": [{"util": 90.0, "mem_used_mb": 800.0}],
            "model_ps": [{"size_vram": 4000}],
        },
        {
            "t": 1.0,
            "cpu": 20.0,
            "mem": {"used_kb": 200},
            "gpu": [{"util": 50.0, "mem_used_mb": 700.0}],
            "model_ps": [{"size_vram": 4200}],
        },
    ]
    s = sampler.summarize(samples)
    assert s.sample_count == 3
    assert s.cpu_peak == 40.0 and s.cpu_mean == 30.0  # None ignored
    assert s.mem_peak_kb == 300
    assert s.gpu_util_peak == 90.0 and s.gpu_util_mean == 50.0
    assert s.vram_peak_mb == 800.0
    assert s.model_vram_peak_bytes == 4200
    assert s.model_load_s == 0.5  # first tick a model appears, > 0


def test_summarize_empty_is_all_none() -> None:
    s = sampler.summarize([])
    assert s.sample_count == 0
    assert s.cpu_peak is None and s.gpu_util_peak is None and s.model_load_s is None


def test_model_load_s_none_when_resident_at_start() -> None:
    # A model already loaded on tick 0 can't be attributed to this turn.
    samples = [
        {"t": 0.0, "model_ps": [{"size_vram": 1}]},
        {"t": 0.5, "model_ps": [{"size_vram": 1}]},
    ]
    assert sampler._model_load_s(samples) is None


def test_sample_binding_path_shape(tmp_path: Path) -> None:
    binding = sampler.SampleBinding(sample_dir=tmp_path / "samples", interval=0.5)
    p = binding.permutation_path(
        suite="aider", task_id="python/anagram", model="ollama/qwen3:latest"
    )
    assert p == tmp_path / "samples" / "aider" / "python-anagram" / "ollama-qwen3-latest.jsonl"
    assert binding.permutation_path(suite="s", task_id="t", model=None).name == "default.jsonl"


def test_sampler_thread_writes_ticks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Stub every backend so the thread is hermetic, then confirm it appends valid JSON.
    monkeypatch.setattr(sampler, "read_cpu_times", lambda: (1, 2))
    monkeypatch.setattr(
        sampler, "read_memory", lambda: {"used_kb": 5, "total_kb": 9, "available_kb": 4}
    )
    monkeypatch.setattr(sampler, "read_gpus", lambda: [])
    monkeypatch.setattr(sampler.ollama, "running_models", lambda *a, **k: [])
    out = tmp_path / "s.jsonl"
    with sampler.ResourceSampler(out_path=out, interval=0.01).running():
        # busy-wait briefly for at least one tick without a foreground sleep
        for _ in range(100000):
            if out.is_file() and out.read_text(encoding="utf-8").strip():
                break
    lines = [line for line in out.read_text(encoding="utf-8").splitlines() if line]
    assert lines, "sampler wrote no ticks"
    first = json.loads(lines[0])
    assert first["cpu"] is None  # first tick: no prev CPU reading
    assert first["mem"] == {"used_kb": 5, "total_kb": 9, "available_kb": 4}


def test_summary_to_dict_is_plain() -> None:
    d = sampler.summary_to_dict(sampler.summarize([]))
    assert d["sample_count"] == 0 and "cpu_peak" in d
