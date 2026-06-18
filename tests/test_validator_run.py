"""Orchestration tests for `run_validate` — the heavy steps are monkeypatched, so
the control flow (tier toggles, --only, baseline, outputs, teardown, --strict) is
exercised without a Docker daemon."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from book_em_danno.commands import sandbox as sb
from book_em_danno.config.schema import CloudBackend, DannoConfig, Model, OllamaBackend
from book_em_danno.core.exec import CaptureResult, CommandFailedError, Runner
from danno_validator import run as run_mod
from danno_validator.driver import OpencodeTurn
from danno_validator.events import ValidateEvent
from danno_validator.level0 import ConversationResult, TurnRecord
from danno_validator.level1 import TaskResult
from danno_validator.level2 import DevTaskResult, TestRun
from danno_validator.matrix import ConfigVariant
from danno_validator.oracle import FailureClass, classify_turn
from danno_validator.run import Reporter, ValidateOptions, run_validate
from danno_validator.sweep import SweepResult

NOW = datetime(2026, 6, 18, 14, 30, 5, tzinfo=UTC)


# --- canned results ---------------------------------------------------------


def _turn(text: str, *, tool: str | None = None) -> OpencodeTurn:
    events: list[dict] = [
        {"type": "text", "sessionID": "s", "part": {"type": "text", "text": text}}
    ]
    if tool is not None:
        events.append(
            {
                "type": "tool",
                "sessionID": "s",
                "part": {"type": "tool", "tool": tool, "state": {"status": "completed"}},
            }
        )
    events.append({"type": "step_finish", "sessionID": "s", "part": {"reason": "stop"}})
    return OpencodeTurn(result=CaptureResult([], 0, "", ""), events=events, raw=text)


def _l0(overall: FailureClass) -> ConversationResult:
    r = ConversationResult(
        model="ollama/x",
        sandbox="box",
        workspace_root=Path("/tmp/ws"),
        session_id="s",
        overall=overall,
    )
    turn = _turn("hi")
    r.records = [
        TurnRecord(
            label="greet",
            prompt="hi",
            turn=turn,
            verdict=classify_turn(turn, side_effect=False, expects_action=False),
            latency_s=1.0,
        )
    ]
    return r


def _l1() -> TaskResult:
    turn = _turn("wrote", tool="bash")
    return TaskResult(
        model="ollama/x",
        sandbox="box",
        workspace_root=Path("/tmp/ws"),
        task_label="line-count",
        session_id="s",
        turn=turn,
        verdict=classify_turn(turn, side_effect=True, expects_action=True),
        latency_s=1.0,
    )


def _l2(*, returncode: int = 0) -> DevTaskResult:
    turn = _turn("impl", tool="edit")
    return DevTaskResult(
        model="ollama/x",
        sandbox="box",
        workspace_root=Path("/tmp/ws"),
        task_label="fizzbuzz",
        session_id="s",
        turn=turn,
        verdict=classify_turn(turn, side_effect=returncode == 0, expects_action=True),
        test_run=TestRun(command="python3 t.py", returncode=returncode, stdout="", stderr=""),
        latency_s=1.0,
    )


def _pass(name: str, ref: str) -> SweepResult:
    return SweepResult(
        variant=ConfigVariant(name, ref, ref),
        result=_l0(FailureClass.PASS),
        level1=_l1(),
        level2=_l2(),
    )


def _stall(name: str, ref: str) -> SweepResult:
    return SweepResult(variant=ConfigVariant(name, ref, ref), result=_l0(FailureClass.STALL))


def _config() -> DannoConfig:
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://h:11434/v1"),
            "cloud": CloudBackend(kind="cloud", provider="anthropic"),
        },
        models={
            "gptoss": Model(backend="ollama", tag="gpt-oss:20b", tool_call=True),
            "gemma": Model(backend="ollama", tag="gemma3:27b"),
            "sonnet": Model(backend="cloud", id="claude-sonnet-4-6"),
        },
    )


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Monkeypatch every Docker-touching step; record the calls."""
    calls: dict = {"provision": [], "stop": [], "rm": [], "sweep_kwargs": {}, "baseline": 0}

    monkeypatch.setattr(run_mod, "prepare_workspace", lambda runner, ws, config: ws)
    monkeypatch.setattr(sb, "provision", lambda r, name, ws, **kw: calls["provision"].append(name))
    monkeypatch.setattr(sb, "stop", lambda r, name: calls["stop"].append(name))
    monkeypatch.setattr(sb, "agent_env", lambda *a, **k: ["TOKEN=x"])

    def fake_sweep(
        runner,
        name,
        *,
        config,
        workspace_root,
        only,
        reset,
        level1,
        level2,
        on_event,
        agent="build",
    ):
        calls["sweep_kwargs"] = {
            "only": only,
            "level1": level1,
            "level2": level2,
            "reset": reset,
            "agent": agent,
        }
        on_event(ValidateEvent(phase="config-start", config="gptoss", model_ref="ollama/x"))
        names = list(only or ["gptoss", "gemma", "sonnet"])
        return [_pass(n, f"ollama/{n}") for n in names]

    def fake_baseline(runner, name, *, workspace_root, model, reset, level1, level2, on_event):
        calls["baseline"] += 1
        return _pass("claude-code", model or "claude-opus-4-8")

    monkeypatch.setattr(run_mod, "run_sweep", fake_sweep)
    monkeypatch.setattr(run_mod, "run_baseline", fake_baseline)
    return calls


def _opts(tmp_path: Path, **kw: object) -> ValidateOptions:
    base = {"target": tmp_path, "out_dir": tmp_path / "out", "workspace": tmp_path / "ws"}
    base.update(kw)
    return ValidateOptions(**base)  # type: ignore[arg-type]


def _run(opts: ValidateOptions) -> object:
    return run_validate(_config(), opts, Runner(apply=True), now=NOW, version="0.3.0")


# --- tests ------------------------------------------------------------------


def test_dry_run_resolves_plan_without_side_effects(patched: dict, tmp_path: Path) -> None:
    result = _run(_opts(tmp_path, dry_run=True))
    assert result.dry_run is True
    assert result.plan.swept_models == ["gemma", "gptoss", "sonnet"]
    assert patched["provision"] == []  # nothing provisioned


def test_max_level_drives_tier_toggles(patched: dict, tmp_path: Path) -> None:
    _run(_opts(tmp_path, max_level=1))
    assert patched["sweep_kwargs"]["level1"] is True
    assert patched["sweep_kwargs"]["level2"] is False


def test_sweep_uses_the_opencode_run_agent_not_the_docker_agent(
    patched: dict, tmp_path: Path
) -> None:
    # The Docker sandbox agent (--agent opencode) must NOT leak in as the opencode
    # run-agent: the sweep drives `opencode run --agent build`, never `--agent opencode`.
    _run(_opts(tmp_path, only=["gptoss"], agent="opencode"))
    assert patched["sweep_kwargs"]["agent"] == "build"


def test_only_passes_through_and_unknown_fails_loud(patched: dict, tmp_path: Path) -> None:
    _run(_opts(tmp_path, only=["gptoss"]))
    assert patched["sweep_kwargs"]["only"] == ["gptoss"]
    with pytest.raises(ValueError, match="nope"):
        _run(_opts(tmp_path, only=["nope"]))


def test_baseline_provisions_claude_and_appends_row(patched: dict, tmp_path: Path) -> None:
    result = _run(_opts(tmp_path, only=["gptoss"], baseline=True, baseline_model="opus"))
    assert patched["baseline"] == 1
    assert any("validate-claude" in n for n in patched["provision"])
    assert result.results[-1].variant.model_name == "claude-code"


def test_baseline_missing_token_fails_before_provisioning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # agent_env raises (no token) — and it must raise before any provision call.
    monkeypatch.setattr(run_mod, "prepare_workspace", lambda *a, **k: None)
    provisioned: list = []
    monkeypatch.setattr(sb, "provision", lambda r, name, ws, **kw: provisioned.append(name))

    def _no_token(*a: object, **k: object) -> list[str]:
        raise CommandFailedError("no token")

    monkeypatch.setattr(sb, "agent_env", _no_token)
    with pytest.raises(CommandFailedError):
        _run(_opts(tmp_path, baseline=True))
    assert provisioned == []


def test_outputs_written_to_run_dir(patched: dict, tmp_path: Path) -> None:
    result = _run(_opts(tmp_path, only=["gptoss"]))
    assert result.index is not None and result.index.is_file()
    assert result.menu_path is not None and result.menu_path.is_file()
    assert result.results_json is not None and result.results_json.is_file()
    assert (tmp_path / "out" / "index.md").is_file()


def test_no_menu_skips_menu(patched: dict, tmp_path: Path) -> None:
    result = _run(_opts(tmp_path, only=["gptoss"], menu=False))
    assert result.menu_path is None


def test_teardown_runs_unless_kept(patched: dict, tmp_path: Path) -> None:
    _run(_opts(tmp_path, only=["gptoss"]))
    assert any("validate" in n for n in patched["stop"])  # torn down
    patched["stop"].clear()
    _run(_opts(tmp_path, only=["gptoss"], keep_sandboxes=True))
    assert patched["stop"] == []  # left up


def test_strict_failed_only_when_strict_and_a_config_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(run_mod, "prepare_workspace", lambda *a, **k: None)
    monkeypatch.setattr(sb, "provision", lambda *a, **k: None)
    monkeypatch.setattr(sb, "stop", lambda *a, **k: None)

    def sweep_with_a_stall(runner, name, *, on_event, **kw):
        return [_pass("gptoss", "ollama/gpt-oss:20b"), _stall("gemma", "ollama/gemma3:27b")]

    monkeypatch.setattr(run_mod, "run_sweep", sweep_with_a_stall)
    assert _run(_opts(tmp_path, strict=True)).strict_failed is True
    assert _run(_opts(tmp_path, strict=False)).strict_failed is False


def test_reporter_receives_plan_events_and_summary(patched: dict, tmp_path: Path) -> None:
    class Rec(Reporter):
        def __init__(self) -> None:
            self.planned = False
            self.events: list[str] = []
            self.summarized = False

        def plan(self, plan: object, *, dry_run: bool) -> None:
            self.planned = True

        def event(self, ev: ValidateEvent) -> None:
            self.events.append(ev.phase)

        def summary(self, result: object) -> None:
            self.summarized = True

    rec = Rec()
    run_validate(
        _config(), _opts(tmp_path, only=["gptoss"]), Runner(apply=True), reporter=rec, now=NOW
    )
    assert rec.planned and rec.summarized
    assert "config-start" in rec.events
