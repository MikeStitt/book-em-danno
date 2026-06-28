"""Tests for `danno benchmark` — candidate-config sweeping.

Unit tests for candidate discovery + the workspace apply, plus a monkeypatched
orchestration test (heavy steps stubbed, so control flow runs without a Docker
daemon — mirrors test_validator_run).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from book_em_danno.commands import sandbox as sb
from book_em_danno.config.schema import DannoConfig, OllamaBackend
from book_em_danno.core.exec import CaptureResult, Runner
from danno_validator import benchmark as bm
from danno_validator.driver import OpencodeTurn
from danno_validator.level0 import ConversationResult, TurnRecord
from danno_validator.matrix import ConfigVariant
from danno_validator.oracle import FailureClass, classify_turn
from danno_validator.sweep import SweepResult

NOW = datetime(2026, 6, 22, 9, 0, 0, tzinfo=UTC)


def _config() -> DannoConfig:
    return DannoConfig(
        backends={"ollama": OllamaBackend(kind="ollama", base_url="http://h:11434/v1")},
    )


def _pass(name: str) -> SweepResult:
    turn = OpencodeTurn(result=CaptureResult([], 0, "", ""), events=[], raw="")
    r = ConversationResult(
        model="",
        sandbox="box",
        workspace_root=Path("/tmp/ws"),
        session_id=None,
        overall=FailureClass.PASS,
    )
    r.records = [
        TurnRecord(
            label="greet",
            prompt="hi",
            turn=turn,
            verdict=classify_turn(turn, side_effect=False, expects_action=False),
            latency_s=1.0,
        )
    ]
    return SweepResult(variant=ConfigVariant(name, "", f"config:{name}"), result=r)


def _make_candidate(root: Path, name: str, jsonc: str = "{}\n") -> None:
    d = root / name / ".opencode"
    d.mkdir(parents=True)
    (d / "opencode.jsonc").write_text(jsonc, encoding="utf-8")


# --- discovery + apply (units) ---------------------------------------------


def test_discover_candidates_lists_opencode_subdirs_sorted(tmp_path: Path) -> None:
    _make_candidate(tmp_path, "beta")
    _make_candidate(tmp_path, "alpha")
    (tmp_path / "not-a-config").mkdir()  # no .opencode → ignored
    (tmp_path / "loose.txt").write_text("x", encoding="utf-8")
    names = [c.name for c in bm.discover_candidates(tmp_path)]
    assert names == ["alpha", "beta"]


def test_discover_candidates_missing_dir_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="not found"):
        bm.discover_candidates(tmp_path / "nope")


def test_discover_candidates_empty_fails_loud(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()  # a subdir without .opencode
    with pytest.raises(ValueError, match="no candidate configs"):
        bm.discover_candidates(tmp_path)


def test_apply_config_replaces_opencode(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".opencode").mkdir(parents=True)
    (ws / ".opencode" / "stale.jsonc").write_text("old", encoding="utf-8")
    cand = tmp_path / "cand"
    (cand / ".opencode").mkdir(parents=True)
    (cand / ".opencode" / "opencode.jsonc").write_text("new", encoding="utf-8")
    bm.apply_config(ws, cand)
    assert (ws / ".opencode" / "opencode.jsonc").read_text() == "new"
    assert not (ws / ".opencode" / "stale.jsonc").exists()  # old config fully replaced


# --- orchestration (monkeypatched) -----------------------------------------


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> dict:
    calls: dict = {"provision": [], "teardown": [], "applied": [], "seq": [], "tiers": []}
    monkeypatch.setattr(bm, "prepare_workspace", lambda runner, ws, config: ws)
    monkeypatch.setattr(sb, "provision", lambda r, name, ws, **kw: calls["provision"].append(name))
    monkeypatch.setattr(bm, "_teardown", lambda r, name: calls["teardown"].append(name))
    monkeypatch.setattr(bm, "reset_workspace", lambda r, name, ws: calls["seq"].append("reset"))

    def fake_apply(ws: Path, cand: Path) -> None:
        calls["applied"].append(cand.name)
        calls["seq"].append(f"apply:{cand.name}")

    monkeypatch.setattr(bm, "apply_config", fake_apply)

    def fake_tiers(runner, sandbox, *, variant, **kw):  # noqa: ANN001
        calls["tiers"].append(variant.model_name)
        calls["seq"].append(f"tiers:{variant.model_name}")
        calls["tiers_model_ref"] = variant.model_ref
        return _pass(variant.model_name)

    monkeypatch.setattr(bm, "run_tiers", fake_tiers)

    def fake_baseline(runner, name, **kw):  # noqa: ANN001
        calls["baseline_judge"] = kw.get("judge")
        return _pass("claude-code")

    monkeypatch.setattr(bm, "run_baseline", fake_baseline)
    monkeypatch.setattr(bm, "write_sweep_report", lambda results, out: ([], out / "index.md"))
    monkeypatch.setattr(bm, "write_results_json", lambda record, path: path)
    monkeypatch.setattr(sb, "agent_env", lambda *a, **k: ["TOKEN=x"])
    return calls


def _opts(tmp_path: Path, configs: Path, **kw: object) -> bm.BenchmarkOptions:
    base = {"configs_dir": configs, "target": tmp_path, "workspace": tmp_path / "ws"}
    base.update(kw)
    return bm.BenchmarkOptions(**base)  # type: ignore[arg-type]


def _run(opts: bm.BenchmarkOptions) -> bm.BenchmarkResult:
    return bm.run_benchmark(_config(), opts, Runner(apply=True), now=NOW, version="0.3.0")


def test_benchmark_rejects_non_opencode_agent(tmp_path: Path) -> None:
    # benchmark compares .opencode/ trees and drives via opencode, so a non-opencode
    # AUT (e.g. claurst) is rejected loud before provisioning anything — it has no
    # candidate-config analog. `danno bench --agent claurst` is the right tool instead.
    configs = tmp_path / "configs"
    _make_candidate(configs, "a")
    with pytest.raises(ValueError, match="only supports --agent opencode"):
        _run(_opts(tmp_path, configs, agent="claurst"))


def test_runs_tiers_per_candidate_no_baseline(patched: dict, tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _make_candidate(configs, "a")
    _make_candidate(configs, "b")
    result = _run(_opts(tmp_path, configs))
    assert patched["tiers"] == ["a", "b"]  # one tiered run per candidate, sorted
    assert len(result.results) == 2  # no baseline row
    assert patched["teardown"]  # disposable sandbox torn down


def test_benchmark_passes_no_model_ref(patched: dict, tmp_path: Path) -> None:
    # The candidate's own opencode.jsonc carries the model, so the variant has an
    # empty model_ref ⇒ run_tiers passes no -m.
    configs = tmp_path / "configs"
    _make_candidate(configs, "a")
    _run(_opts(tmp_path, configs))
    assert patched["tiers_model_ref"] == ""


def test_resets_then_applies_before_each_run(patched: dict, tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _make_candidate(configs, "a")
    _run(_opts(tmp_path, configs))
    # per candidate: guarded reset → apply that candidate's config → run the battery
    assert patched["seq"][:3] == ["reset", "apply:a", "tiers:a"]


def test_baseline_row_appended(patched: dict, tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _make_candidate(configs, "a")
    result = _run(_opts(tmp_path, configs, baseline=True))
    assert "claude-code" in [s.variant.model_name for s in result.results]
    assert any("claude" in n for n in patched["provision"])


def test_dry_run_no_side_effects(patched: dict, tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _make_candidate(configs, "a")
    _make_candidate(configs, "b")
    result = _run(_opts(tmp_path, configs, dry_run=True))
    assert result.dry_run is True
    assert result.plan.candidates == ["a", "b"]
    assert patched["provision"] == [] and patched["tiers"] == []


def test_keep_sandboxes_skips_teardown(patched: dict, tmp_path: Path) -> None:
    configs = tmp_path / "configs"
    _make_candidate(configs, "a")
    _run(_opts(tmp_path, configs, keep_sandboxes=True))
    assert patched["teardown"] == []
