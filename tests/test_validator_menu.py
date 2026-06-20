"""Unit tests for the annotated "menu" danno.toml emitter.

Pure rendering plus one round-trip that exercises the emitted file through the real
loader (Configuration-is-Code: the menu must be a loadable danno.toml, not just a
plausible-looking string).
"""

from __future__ import annotations

from pathlib import Path

from book_em_danno.config.loader import load_config
from book_em_danno.config.schema import (
    AgentSpec,
    DannoConfig,
    Model,
    NpmPlugin,
    OllamaBackend,
    Tool,
)
from book_em_danno.core.exec import CaptureResult
from danno_validator.driver import OpencodeTurn
from danno_validator.level0 import ConversationResult, TurnRecord
from danno_validator.level1 import TaskResult
from danno_validator.level2 import DevTaskResult, TestRun
from danno_validator.matrix import ConfigVariant
from danno_validator.menu import (
    is_recommended,
    render_menu,
    verdict_badge,
    write_menu,
)
from danno_validator.oracle import FailureClass, classify_turn
from danno_validator.sweep import SweepResult


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


def _l0(model: str, overall: FailureClass) -> ConversationResult:
    r = ConversationResult(
        model=model,
        sandbox="box",
        workspace_root=Path("/tmp/ws"),
        session_id="s",
        overall=overall,
    )
    rec = TurnRecord(label="greet", prompt="hi", turn=_turn("hi"), verdict=None, latency_s=1.0)  # type: ignore[arg-type]
    r.records = [rec]
    return r


def _l1(model: str, *, side_effect: bool) -> TaskResult:
    turn = _turn("wrote it", tool="bash")
    return TaskResult(
        model=model,
        sandbox="box",
        workspace_root=Path("/tmp/ws"),
        task_label="line-count",
        session_id="s",
        turn=turn,
        verdict=classify_turn(turn, side_effect=side_effect, expects_action=True),
        latency_s=1.0,
    )


def _l2(model: str, *, side_effect: bool, returncode: int = 0) -> DevTaskResult:
    turn = _turn("implemented it", tool="edit")
    return DevTaskResult(
        model=model,
        sandbox="box",
        workspace_root=Path("/tmp/ws"),
        task_label="fizzbuzz",
        session_id="s",
        turn=turn,
        verdict=classify_turn(turn, side_effect=side_effect, expects_action=True),
        test_run=TestRun(command="python3 t.py", returncode=returncode, stdout="", stderr=""),
        latency_s=1.0,
    )


def _variant(name: str, ref: str = "ollama/x") -> ConfigVariant:
    return ConfigVariant(name, ref, ref)


def _all_pass(name: str, ref: str) -> SweepResult:
    return SweepResult(
        variant=_variant(name, ref),
        result=_l0(ref, FailureClass.PASS),
        level1=_l1(ref, side_effect=True),
        level2=_l2(ref, side_effect=True),
    )


def _l0_stall(name: str, ref: str) -> SweepResult:
    # L0 stalls → L1/L2 never run (short-circuit), so they stay None.
    return SweepResult(variant=_variant(name, ref), result=_l0(ref, FailureClass.STALL))


def _l2_fail(name: str, ref: str) -> SweepResult:
    return SweepResult(
        variant=_variant(name, ref),
        result=_l0(ref, FailureClass.PASS),
        level1=_l1(ref, side_effect=True),
        level2=_l2(ref, side_effect=False, returncode=1),
    )


# --- verdict_badge / is_recommended -----------------------------------------


def test_verdict_badge_all_pass() -> None:
    assert verdict_badge(_all_pass("m", "ollama/m")) == "[L0 ✓ · L1 ✓ · L2 ✓]"


def test_verdict_badge_l0_stall_skips_higher_tiers() -> None:
    # The skipped tiers read as "– not run", not as failures.
    assert verdict_badge(_l0_stall("m", "ollama/m")) == "[L0 ✗ stall · L1 – · L2 –]"


def test_verdict_badge_l2_failure_names_the_class() -> None:
    assert verdict_badge(_l2_fail("m", "ollama/m")) == "[L0 ✓ · L1 ✓ · L2 ✗ early-stop]"


def test_is_recommended_only_when_all_tiers_pass() -> None:
    assert is_recommended(_all_pass("m", "ollama/m")) is True
    assert is_recommended(_l2_fail("m", "ollama/m")) is False
    assert is_recommended(_l0_stall("m", "ollama/m")) is False


# --- render_menu ------------------------------------------------------------


def _config() -> DannoConfig:
    return DannoConfig(
        backends={
            "ollama": OllamaBackend(kind="ollama", base_url="http://host.docker.internal:11434/v1"),
        },
        models={
            "gemma3-27b": Model(backend="ollama", tag="gemma3:27b", reasoning_effort="none"),
            "gptoss": Model(backend="ollama", tag="gpt-oss:20b", tool_call=True),
            "qwen": Model(backend="ollama", tag="qwen3-coder-next", tool_call=True),
        },
        # coder is a raw inline cloud ref (not a [models] entry, never swept).
        agents={"plan": "gptoss", "coder": "anthropic/claude-sonnet-4-6"},
        tools=[
            Tool(name="ados", source="https://example.com/ados", install_to="sandbox"),
        ],
        npm=[
            NpmPlugin(
                package="opencode-planner",
                config={"workflow": "plan-agent"},
                setup=["echo hi"],
            ),
        ],
    )


def _results() -> list[SweepResult]:
    # gptoss clears everything; gemma3-27b stalls at L0; qwen is declared but NOT
    # swept; a Claude baseline row that the menu must drop.
    return [
        _l0_stall("gemma3-27b", "ollama/gemma3:27b"),
        _all_pass("gptoss", "ollama/gpt-oss:20b"),
        _all_pass("claude-code", "claude-opus-4-8"),
    ]


def test_menu_annotates_each_model_with_its_verdict() -> None:
    menu = render_menu(_config(), _results())
    assert "[L0 ✗ stall · L1 – · L2 –]\n[models.gemma3-27b]" in menu
    assert "[L0 ✓ · L1 ✓ · L2 ✓]  RECOMMENDED\n[models.gptoss]" in menu


def test_menu_marks_unswept_model_not_validated() -> None:
    menu = render_menu(_config(), _results())
    assert "# [not validated — outside the swept set]\n[models.qwen]" in menu


def test_menu_excludes_claude_baseline_row() -> None:
    menu = render_menu(_config(), _results())
    # The baseline is a reference, not a declarable model — it must not appear.
    assert "claude-opus-4-8" not in menu
    assert "claude-code" not in menu


def test_menu_agents_block_is_a_comment_uncomment_menu() -> None:
    menu = render_menu(_config(), _results())
    # The active assignment carries its model's verdict.
    assert 'plan = "gptoss"   # [L0 ✓ · L1 ✓ · L2 ✓]' in menu
    # A raw inline ref (not a [models] entry) is rendered verbatim, marked unvalidated.
    assert 'coder = "anthropic/claude-sonnet-4-6"   # [not validated]' in menu
    # Every other model appears as a commented alternative under the role.
    assert '# plan = "gemma3-27b"   # [L0 ✗ stall · L1 – · L2 –] — uncomment to use' in menu
    assert '# plan = "qwen"   # [not validated] — uncomment to use' in menu


def test_menu_reads_model_from_a_rich_agent_spec() -> None:
    # The menu is a model-selection surface: a rich [agents.<name>] agent's selected
    # model (its `model` field) drives the active line and its verdict badge.
    config = _config()
    config.agents["coder"] = AgentSpec(model="gptoss", mode="subagent")
    menu = render_menu(config, _results())
    assert 'coder = "gptoss"   # [L0 ✓ · L1 ✓ · L2 ✓]' in menu


def test_menu_verified_stamp_is_optional() -> None:
    assert "verified" not in render_menu(_config(), _results())
    stamped = render_menu(_config(), _results(), verified="2026-06-18")
    assert "RECOMMENDED  — verified 2026-06-18" in stamped


def test_menu_round_trips_through_the_loader(tmp_path: Path) -> None:
    # Configuration-is-Code: the emitted menu must be a real, loadable danno.toml.
    config = _config()
    path = write_menu(config, _results(), tmp_path / "menu.danno.toml")
    assert path.is_file()
    loaded = load_config(path)
    assert loaded == config


def test_write_menu_creates_parent_dirs(tmp_path: Path) -> None:
    path = write_menu(_config(), _results(), tmp_path / "nested" / "out" / "menu.toml")
    assert path.is_file()
    assert "[models.gptoss]" in path.read_text()
