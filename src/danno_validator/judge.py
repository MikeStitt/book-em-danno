"""The Level-2 dev-quality judge: a host-side administrator AI graded *on top of*
the objective oracle.

The hidden-test suite (`level2.run_tests`) is the **primary** verdict — it decides
pass/fail deterministically and feeds the `FailureClass` taxonomy. This judge adds
the *fuzzy* layer the plan reserved for it: software-dev quality **beyond**
pass/fail — code clarity and whether the solution is right-sized (not over- or
under-built). It never overrides the oracle; a model can pass the tests and still
score low on clarity, or fail them and still earn partial credit for a clean
attempt. (L0 liveness and L1 tool-use keep the objective oracle only; grading those
is a possible later extension.)

The design mirrors the harness's I/O-at-the-boundary rule so `ninja check` stays
offline:

- **Pure core** — `build_prompt` (work → system/user prompt) and `parse_judgement`
  (model text → validated `Judgement`) read only their arguments, so they are
  unit-testable without a network. `parse_judgement` is where the 1–5 range and
  enum checks live (the Claude structured-output schema can't enforce numeric
  bounds), so it fails loud on a malformed verdict rather than inventing a score.
- **Thin client seam** — `JudgeClient` is a one-method protocol; tests pass a fake
  that returns canned JSON. `AnthropicJudgeClient` is the only thing that touches
  the network, and it **lazy-imports** `anthropic` so importing this module (and
  running the offline tests) never needs the SDK. The SDK ships in the
  `danno[validator]` extra; a missing install fails loud with the install hint.

The judge model is **configurable** (`opus`/`sonnet`/`haiku` via the Claude API)
and **recorded on every `Judgement`** (`Judgement.model`), the same pin-and-track
discipline as the M5 Claude baseline — so a report always says which model graded.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

# Claude model ids for the judge (pinned; see the claude-api reference). The judge
# grades code quality, so the default is the most capable Opus-tier model; the
# caller may pick a cheaper one. Aliases mirror the M5 baseline's pin-and-track.
JUDGE_MODEL_ALIASES = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
}
DEFAULT_JUDGE_MODEL = JUDGE_MODEL_ALIASES["opus"]


def resolve_judge_model(model: str) -> str:
    """Map an alias (`opus`/`sonnet`/`haiku`) to its model id; pass ids through.

    Lets a caller (or a future CLI flag) say `--judge-model sonnet` while the
    recorded model stays the concrete id the run actually used.
    """
    return JUDGE_MODEL_ALIASES.get(model, model)


class JudgeError(RuntimeError):
    """Raised when the judge's response can't be parsed into a valid `Judgement`.

    A malformed verdict is surfaced loudly (Working Rule 8) rather than silently
    coerced into a default score that would misrepresent the model under test.
    """


class Sizing(StrEnum):
    """How well the solution's scope fits the task — the over-/under-build axis."""

    RIGHT_SIZED = "right-sized"
    OVER_BUILT = "over-built"  # needless abstraction, extra files, speculative code
    UNDER_BUILT = "under-built"  # missing cases, stubbed-out behaviour, too thin


@dataclass(frozen=True)
class DevWork:
    """The material the judge grades for one Level-2 attempt.

    Deliberately primitive (no `level2` types) so `judge.py` has no dependency on
    `level2.py` — `level2` imports the judge, not the other way round. `sources`
    pairs each task source filename with the content the agent left on disk
    (`None` if the file is absent after the turn). `test_passed`/`test_output` are
    the objective oracle's result, given to the judge as context — it grades
    quality, the oracle already decided pass/fail.
    """

    prompt: str
    sources: tuple[tuple[str, str | None], ...]
    test_passed: bool
    test_output: str


@dataclass(frozen=True)
class Judgement:
    """The judge's fuzzy verdict on dev quality, layered over the oracle's pass/fail.

    `model` records which Claude model produced it (pin-and-track). `score` and
    `clarity` are 1–5 (validated in `parse_judgement`); `sizing` is the over-/
    under-build axis; `rationale` is the judge's one-paragraph justification.
    """

    model: str
    score: int
    clarity: int
    sizing: Sizing
    rationale: str


# Structured-output schema for the Claude Messages API (`output_config.format`).
# Numeric bounds (1–5) are NOT expressible here — the API rejects min/max in
# structured outputs — so `parse_judgement` enforces the range. The schema only
# guarantees the shape and the `sizing` enum.
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {
            "type": "integer",
            "description": "overall dev quality, 1 (poor) to 5 (excellent)",
        },
        "clarity": {
            "type": "integer",
            "description": "code readability, 1 (poor) to 5 (excellent)",
        },
        "sizing": {"type": "string", "enum": [s.value for s in Sizing]},
        "rationale": {"type": "string", "description": "one paragraph justifying the scores"},
    },
    "required": ["score", "clarity", "sizing", "rationale"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """\
You are a senior engineer grading the *quality* of a small coding task a model \
under test completed. A hidden test suite has ALREADY decided pass/fail \
objectively — that verdict is final and is not your job. You grade quality \
*beyond* pass/fail: how clear and well-structured the code is, and whether its \
scope fits the task (not over-built with needless abstraction or speculative \
code, not under-built with missing cases or stubs).

Score on a 1–5 scale (1 = poor, 5 = excellent):
- score: overall dev quality.
- clarity: readability — naming, structure, absence of confusion.
Also classify sizing as exactly one of: right-sized, over-built, under-built.

A solution can pass the tests yet read poorly (low clarity), or fail them yet \
show a clean, well-sized attempt (partial credit). Judge what is on disk, not the \
test result. Respond with ONLY the JSON object the schema describes."""

# A `JudgeFn` is what the level runners call: given the work, return a Judgement.
# `make_judge` binds a client + model into one. None at the runner means no judge.
JudgeFn = Callable[[DevWork], "Judgement"]


class JudgeClient(Protocol):
    """The one-method seam the judge needs from an LLM backend.

    `complete` takes a system + user prompt and returns the model's text (expected
    to be the JSON `parse_judgement` reads). Structurally satisfied by
    `AnthropicJudgeClient` and by test fakes — no inheritance, mockable offline.
    """

    def complete(self, *, model: str, system: str, prompt: str) -> str: ...


def _fmt_sources(sources: tuple[tuple[str, str | None], ...]) -> str:
    """Render the produced source files for the prompt (absent files flagged)."""
    blocks = []
    for name, content in sources:
        body = content if content is not None else "(file absent — the agent did not create it)"
        blocks.append(f"### `{name}`\n```\n{body}\n```")
    return "\n\n".join(blocks)


def build_prompt(work: DevWork) -> tuple[str, str]:
    """Build the (system, user) prompt pair for one `DevWork`. Pure."""
    outcome = "passed" if work.test_passed else "failed"
    test_output = work.test_output.strip() or "(no output)"
    user = (
        "## Task given to the model\n"
        f"{work.prompt}\n\n"
        "## Code the model left on disk\n"
        f"{_fmt_sources(work.sources)}\n\n"
        "## Objective hidden-test result (already decided — context only)\n"
        f"The hidden test suite **{outcome}**. Test output:\n```\n{test_output}\n```\n\n"
        "Grade the dev quality per your instructions and return the JSON verdict."
    )
    return _SYSTEM_PROMPT, user


def _check_score(payload: dict, key: str) -> int:
    """Pull a 1–5 integer from the judge payload or raise `JudgeError`."""
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 5:
        raise JudgeError(f"judge {key!r} must be an integer 1–5, got {value!r}")
    return value


def parse_judgement(text: str, *, model: str) -> Judgement:
    """Parse + validate the judge's response into a `Judgement`. Pure.

    Tolerates a ```json fence (defensive — `output_config.format` returns raw JSON,
    but a different/mock backend might fence it). Validates the 1–5 range and the
    `sizing` enum the structured-output schema can't enforce, failing loud on
    anything malformed rather than guessing a score.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Drop the opening fence (``` or ```json) and the closing fence.
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[: -len("```")]
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise JudgeError(f"judge response was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise JudgeError(f"judge response must be a JSON object, got {type(payload).__name__}")

    score = _check_score(payload, "score")
    clarity = _check_score(payload, "clarity")
    sizing_raw = payload.get("sizing")
    try:
        if not isinstance(sizing_raw, str):
            raise ValueError
        sizing = Sizing(sizing_raw)
    except ValueError as exc:
        raise JudgeError(
            f"judge 'sizing' must be one of {[s.value for s in Sizing]}, got {sizing_raw!r}"
        ) from exc
    rationale = payload.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise JudgeError(f"judge 'rationale' must be a non-empty string, got {rationale!r}")

    return Judgement(model=model, score=score, clarity=clarity, sizing=sizing, rationale=rationale)


def judge_dev_quality(client: JudgeClient, work: DevWork, *, model: str) -> Judgement:
    """Grade one `DevWork`: build the prompt, ask the client, parse the verdict."""
    system, prompt = build_prompt(work)
    text = client.complete(model=model, system=system, prompt=prompt)
    return parse_judgement(text, model=model)


def make_judge(client: JudgeClient, *, model: str = DEFAULT_JUDGE_MODEL) -> JudgeFn:
    """Bind a `client` + `model` into a `JudgeFn` for the level runners.

    `model` may be an alias (`opus`/`sonnet`/`haiku`) or a concrete id; it is
    resolved once here so the recorded `Judgement.model` is the real id.
    """
    resolved = resolve_judge_model(model)

    def judge(work: DevWork) -> Judgement:
        return judge_dev_quality(client, work, model=resolved)

    return judge


def _make_anthropic_client() -> object:
    """Construct the Anthropic SDK client, failing loud if the extra isn't installed.

    Lazy-imported so this module (and the offline tests) never require the SDK; the
    `anthropic` dep lives in the `danno[validator]` extra.
    """
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise JudgeError(
            "the L2 dev-quality judge needs the Anthropic SDK; install it with "
            "`pip install danno[validator]` (or `uv sync --extra validator`)."
        ) from exc
    return anthropic.Anthropic()


class AnthropicJudgeClient:
    """`JudgeClient` backed by the Claude Messages API (the only networked piece).

    Uses structured outputs (`output_config.format`) so the response is guaranteed
    valid JSON in the judge's shape; `parse_judgement` still enforces the numeric
    bounds the schema can't. Auth resolves from the host environment the SDK's
    usual way (`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`).
    """

    def __init__(self, client: object | None = None) -> None:
        self._client = client if client is not None else _make_anthropic_client()

    def complete(self, *, model: str, system: str, prompt: str) -> str:
        resp = self._client.messages.create(  # type: ignore[attr-defined]
            model=model,
            max_tokens=1024,
            system=system,
            output_config={"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if block.type == "text":
                return str(block.text)
        raise JudgeError("judge response contained no text block")
