"""V5 — drift detectors (GV3, Tier B, opt-in).

The gates sit on drifting ground: the sandbox template is unpinned (an opencode V1→V2
runner cutover changes `agent.steps` semantics), and the stub's wire framing can diverge
from a live backend. These tests name the drift when it happens rather than letting a gate
silently change meaning.

- **steps canary** — seed `agent.steps=N`, run a forever-loop stub cell, and assert on the
  number of WIRE round-trips the stub saw (not exit semantics — at-cap behavior differs
  across opencode versions: summarize-prompt vs tools-disabled vs StepLimitExceededError).
  If the count stops obeying `steps`, the template flipped runners — the message says so.
- **provenance completeness** — provenance.json must record the resolved gates. The harness
  VERSION + per-cell resolved gates are the F5 follow-up (not fixed in GV1); asserted-loose
  here and flagged.
- **stub-vs-live framing diff** — opt-in (needs a pulled Ollama model): replay one exchange
  against real Ollama and diff the SSE/NDJSON framing against the stub's, the stub-fidelity
  guard.

⚠️ NOT YET LIVE-VERIFIED — see `gates_fixtures` module docstring.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from gates_fixtures import (
    LOOP_TOOL,
    LOOP_TOOL_ARGS,
    MODEL_TAG,
    OLLAMA_DOWN,
    STUB_PORT,
    model_present,
    provisioned_sandbox,
    requires_docker,
    run_scripted_turn,
    scripted_backend,
)

from book_em_danno.core.exec import Runner
from book_em_danno.stubai import Finish, StubConfig, ToolLoop, stub_ai
from danno_validator.suites.bench import _seed_opencode_config
from danno_validator.suites.config import ResolvedGates

pytestmark = [pytest.mark.slow, requires_docker]

_STEPS = 4


@pytest.mark.timeout(900)
def test_opencode_steps_cap_is_honored_on_the_wire(tmp_path: Path) -> None:
    # Seed agent.steps=_STEPS, then let the stub loop forever. A V1-runner opencode honors
    # `steps ?? Infinity` and stops after ~_STEPS round-trips; if the template flipped to the
    # V2 runner (hardcoded MAX_STEPS, or steps ignored), the wire count diverges — which is
    # exactly the signal this canary exists to raise.
    name = "danno-gates-steps"
    with provisioned_sandbox(name, "opencode", tmp_path) as workspace:
        # Re-seed the opencode config with the step cap (bench does this per run).
        from gates_fixtures import gen_config

        _seed_opencode_config(gen_config("opencode"), "opencode", workspace, run_agent_steps=_STEPS)
        gates = ResolvedGates(max_turns=1000, max_tokens=10_000_000, timeout_s=600.0)
        with scripted_backend([ToolLoop(LOOP_TOOL, LOOP_TOOL_ARGS, n=None)], tmp_path) as backend:
            _turn, watch = run_scripted_turn(
                Runner(apply=True),
                name,
                backend,
                "loop",
                harness="opencode",
                gates=gates,
                workspace=workspace,
            )
            rounds = backend.tally.inference_calls()
    assert watch.breach is None, "steps should stop the loop before any external gate fires"
    assert rounds <= _STEPS + 1, (
        f"opencode made {rounds} wire round-trips with agent.steps={_STEPS} — the sandbox "
        "template's session runner no longer honors `steps` (likely a V1→V2 cutover). "
        "Re-verify the step-cap semantics and danno's jsonc schema before trusting option B."
    )


@pytest.mark.timeout(1200)
def test_provenance_records_the_gates(tmp_path: Path) -> None:
    (tmp_path / "danno.toml").write_text(
        "[defaults]\n"
        'default_agent = "build"\n\n'
        "[backends.ollama]\n"
        'kind = "ollama"\n'
        f'base_url = "http://host.docker.internal:{STUB_PORT}/v1"\n\n'
        "[models.stub]\n"
        'backend = "ollama"\n'
        f'tag = "{MODEL_TAG}"\n'
        'reasoning_effort = "none"\n\n'
        "[agents]\n"
        'build = "stub"\n',
        encoding="utf-8",
    )
    (tmp_path / "benchmarks.toml").write_text(
        '[aider_polyglot]\nenabled = true\nselect = ["python/anagram"]\n\n'
        "[gates]\nmax_turns = 5\ntimeout_s = 300\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"
    with stub_ai(
        StubConfig(script=[Finish("done")], transcript_file=tmp_path / "s.jsonl", port=STUB_PORT)
    ):
        proc = subprocess.run(
            ["danno", "bench", "--target", str(tmp_path), "--no-save-captures", "--out", str(out)],
            capture_output=True,
            text=True,
            timeout=1100,
            check=False,
        )
    assert proc.returncode == 0, proc.stderr
    provenance = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    # The gates block is recorded today (bb93333). F5 follow-up: per-cell RESOLVED gates on
    # each verdict row + the harness VERSION — assert those once F5 lands.
    assert "gates" in provenance, "provenance must record the runaway-gate caps"
    assert provenance["gates"].get("max_turns") == 5


@pytest.mark.skipif(OLLAMA_DOWN or not model_present("gemma4:26b"), reason="no live Ollama model")
@pytest.mark.timeout(300)
def test_stub_framing_matches_live_ollama() -> None:
    # Stub-fidelity guard: the stub's chat-completions SSE framing should carry the same
    # structural markers a live Ollama `/v1` stream does (role/content deltas, a usage-less
    # or usage-bearing tail, `[DONE]`). A cheap structural diff, not byte-equality.
    import urllib.request

    from book_em_danno.capture.usage import extract_usage

    live = _live_chat_sse("gemma4:26b", "Reply with the single word: ping")
    with stub_ai(
        StubConfig(
            script=[Finish("ping")],
            transcript_file=Path(tempfile.mkdtemp()) / "s.jsonl",
            port=STUB_PORT,
        )
    ) as stub:
        req = urllib.request.Request(
            f"{stub.base_url}/v1/chat/completions",
            data=json.dumps(
                {"model": MODEL_TAG, "stream": True, "stream_options": {"include_usage": True}}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        stub_sse = urllib.request.urlopen(req, timeout=30).read().decode()
    assert stub_sse.rstrip().endswith("data: [DONE]") == live.rstrip().endswith("data: [DONE]")
    assert (extract_usage(stub_sse) is not None) == (extract_usage(live) is not None)


def _live_chat_sse(model: str, prompt: str) -> str:
    import urllib.request

    from book_em_danno.commands import ollama

    req = urllib.request.Request(
        f"{ollama.DEFAULT_HOST_URL}/v1/chat/completions",
        data=json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=60).read().decode()
