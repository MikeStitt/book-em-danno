"""Unit tests for the L2 dev-quality judge. The pure core (prompt building, parsing
+ validation) is exercised directly; the orchestrator runs over a fake `JudgeClient`
so no network is touched. The Anthropic seam is only checked for its fail-loud
behaviour when the SDK is absent — the real API call is live-verified separately."""

from __future__ import annotations

import json

import pytest

from danno_validator.judge import (
    DEFAULT_JUDGE_MODEL,
    AnthropicJudgeClient,
    DevWork,
    JudgeError,
    Judgement,
    Sizing,
    build_prompt,
    judge_dev_quality,
    make_judge,
    parse_judgement,
    resolve_judge_model,
)

_WORK = DevWork(
    prompt="Implement fizzbuzz(n).",
    sources=(("fizzbuzz.py", "def fizzbuzz(n): return str(n)"),),
    test_passed=True,
    test_output="ok — 12 cases passed",
)


def _verdict_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "score": 4,
        "clarity": 5,
        "sizing": "right-sized",
        "rationale": "Clean, direct implementation.",
    }
    payload.update(overrides)
    return json.dumps(payload)


class _FakeClient:
    """A `JudgeClient` that records its call and returns canned text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, str]] = []

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        self.calls.append({"model": model, "system": system, "prompt": prompt})
        return self.text


# --- model resolution ---------------------------------------------------------


def test_resolve_judge_model_maps_aliases() -> None:
    assert resolve_judge_model("opus") == "claude-opus-4-8"
    assert resolve_judge_model("sonnet") == "claude-sonnet-4-6"
    assert resolve_judge_model("haiku") == "claude-haiku-4-5"


def test_resolve_judge_model_passes_through_concrete_ids() -> None:
    assert resolve_judge_model("claude-opus-4-8") == "claude-opus-4-8"
    assert DEFAULT_JUDGE_MODEL == "claude-opus-4-8"


# --- build_prompt (pure) ------------------------------------------------------


def test_build_prompt_includes_contract_sources_and_outcome() -> None:
    system, user = build_prompt(_WORK)
    assert "grading the *quality*" in system
    assert "Implement fizzbuzz(n)." in user
    assert "def fizzbuzz(n): return str(n)" in user
    assert "`fizzbuzz.py`" in user
    assert "**passed**" in user
    assert "12 cases passed" in user


def test_build_prompt_flags_absent_files_and_failure() -> None:
    work = DevWork(
        prompt="do it",
        sources=(("missing.py", None),),
        test_passed=False,
        test_output="",
    )
    _system, user = build_prompt(work)
    assert "file absent" in user
    assert "**failed**" in user
    assert "(no output)" in user


# --- parse_judgement (pure) ---------------------------------------------------


def test_parse_judgement_valid() -> None:
    j = parse_judgement(_verdict_json(), model="claude-opus-4-8")
    assert j == Judgement(
        model="claude-opus-4-8",
        score=4,
        clarity=5,
        sizing=Sizing.RIGHT_SIZED,
        rationale="Clean, direct implementation.",
    )


def test_parse_judgement_strips_code_fence() -> None:
    fenced = f"```json\n{_verdict_json()}\n```"
    j = parse_judgement(fenced, model="m")
    assert j.score == 4 and j.sizing is Sizing.RIGHT_SIZED


def test_parse_judgement_rejects_non_json() -> None:
    with pytest.raises(JudgeError, match="not valid JSON"):
        parse_judgement("the code looks fine to me", model="m")


def test_parse_judgement_rejects_non_object() -> None:
    with pytest.raises(JudgeError, match="must be a JSON object"):
        parse_judgement("[1, 2, 3]", model="m")


@pytest.mark.parametrize("bad", [0, 6, -1, "4", 3.5, True, None])
def test_parse_judgement_rejects_out_of_range_score(bad: object) -> None:
    with pytest.raises(JudgeError, match="'score' must be an integer 1–5"):
        parse_judgement(_verdict_json(score=bad), model="m")


def test_parse_judgement_rejects_out_of_range_clarity() -> None:
    with pytest.raises(JudgeError, match="'clarity' must be an integer 1–5"):
        parse_judgement(_verdict_json(clarity=9), model="m")


def test_parse_judgement_rejects_unknown_sizing() -> None:
    with pytest.raises(JudgeError, match="'sizing' must be one of"):
        parse_judgement(_verdict_json(sizing="enormous"), model="m")


def test_parse_judgement_rejects_empty_rationale() -> None:
    with pytest.raises(JudgeError, match="'rationale' must be a non-empty string"):
        parse_judgement(_verdict_json(rationale="   "), model="m")


# --- orchestrator over a fake client ------------------------------------------


def test_judge_dev_quality_calls_client_and_parses() -> None:
    client = _FakeClient(_verdict_json(score=3, sizing="over-built"))
    j = judge_dev_quality(client, _WORK, model="claude-sonnet-4-6")
    assert j.model == "claude-sonnet-4-6"
    assert j.score == 3 and j.sizing is Sizing.OVER_BUILT
    # The client saw the built prompt and the pinned model.
    assert client.calls[0]["model"] == "claude-sonnet-4-6"
    assert "Implement fizzbuzz(n)." in client.calls[0]["prompt"]


def test_make_judge_binds_resolved_model() -> None:
    client = _FakeClient(_verdict_json())
    judge = make_judge(client, model="sonnet")
    j = judge(_WORK)
    # The alias was resolved to the concrete id and recorded on the verdict.
    assert j.model == "claude-sonnet-4-6"
    assert client.calls[0]["model"] == "claude-sonnet-4-6"


def test_make_judge_default_model() -> None:
    client = _FakeClient(_verdict_json())
    make_judge(client)(_WORK)
    assert client.calls[0]["model"] == DEFAULT_JUDGE_MODEL


# --- Anthropic seam (offline behaviour only) ----------------------------------


def test_anthropic_client_complete_extracts_text_block() -> None:
    class _Block:
        type = "text"
        text = _verdict_json()

    class _Resp:
        content = [_Block()]

    class _Messages:
        def create(self, **kwargs: object) -> _Resp:
            assert kwargs["model"] == "claude-opus-4-8"
            assert "output_config" in kwargs
            return _Resp()

    class _SDK:
        messages = _Messages()

    client = AnthropicJudgeClient(client=_SDK())
    text = client.complete(model="claude-opus-4-8", system="s", prompt="p")
    assert parse_judgement(text, model="claude-opus-4-8").score == 4


def test_anthropic_client_raises_when_no_text_block() -> None:
    class _Resp:
        content: list[object] = []

    class _Messages:
        def create(self, **kwargs: object) -> _Resp:
            return _Resp()

    class _SDK:
        messages = _Messages()

    client = AnthropicJudgeClient(client=_SDK())
    with pytest.raises(JudgeError, match="no text block"):
        client.complete(model="m", system="s", prompt="p")
