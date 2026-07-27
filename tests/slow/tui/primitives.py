"""Reusable TUI primitives (proven in the 2026-07-21 spikes), rewritten against `TuiDriver`.

Everything here calls only the `TuiDriver` surface (`pump`/`screen`/`send`/`enter`), never a
pty library, so it is backend-agnostic (`.docs/plan-slow-sandbox-tui-tests.md` §6/§8.2):

- **`HARNESS`** — the per-harness marker table (banner/composer/dialog/summ + inflate), lifted
  verbatim from the spike so the drivers know what "reached the TUI" and "compacted" look like.
- **`settle_and_dismiss`** — a fixed settle window that ESC/answers each known first-run modal,
  crucially catching opencode's LATE auto-update modal (ESC=Skip; never Enter — Enter confirms
  the update and EOFs the process mid-run).
- **`submit`** — the wire-confirmed input primitive: type + Enter, then CONFIRM a new request
  landed on the capture proxy, retrying if a racing overlay ate the Enter.
- **`one_shot_inflate`** — inflate the stub's `prompt_tokens` on the FIRST reply only, so the
  harness sees exactly one over-budget turn (inflating every reply → unbounded compaction
  runaway: opencode did 1184 reqs / 592 summarizations in ~67s).
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .driver import TuiDriver


class _WireLike(Protocol):
    """The slice of `fixtures.WireMetrics` `submit` needs — duck-typed to avoid an import
    cycle (fixtures imports this module for `HarnessTui`)."""

    def requests(self) -> int: ...


@dataclass(frozen=True)
class HarnessTui:
    """Per-harness TUI knobs: what its banner/composer/first-run-modals/compaction look like on
    the wire and screen, plus the one-shot usage inflation magnitude. One row per harness."""

    name: str
    model: str  # the `-m` ref for `sandbox.launch` (codex/opencode: "stub"; claurst: "ollama/stub")
    inflate: int  # first-reply prompt_tokens to force one compaction decision
    wire_path: str  # the inference endpoint suffix ("/chat/completions" | "/responses")
    banner: tuple[str, ...]  # any-of markers proving the TUI painted
    composer: tuple[str, ...]  # any-of markers proving the input composer is ready
    dialogs: tuple[tuple[str, str], ...]  # (screen-marker, keys-to-send) first-run modal handlers
    summ_markers: tuple[str, ...]  # any-of substrings proving a summarization/compaction request
    graft_compact_limit: bool = False  # codex needs the top-level model_auto_compact_token_limit


# Lifted verbatim from `scratchpad/spike_harness.py` (all three A/H/C green on macOS 2026-07-21).
HARNESS: dict[str, HarnessTui] = {
    "codex": HarnessTui(
        name="codex",
        model="stub",
        inflate=50_000,  # >> the small model_auto_compact_token_limit grafted in (§8.3)
        wire_path="/responses",
        banner=("welcome to codex", ">_ openai codex"),
        composer=("/model to change",),  # THIS codex build's composer line (not "Ask Codex")
        dialogs=(("do you trust", "1\r"),),  # first-run trust dialog: '1' + Enter (Yes, continue)
        summ_markers=(
            # codex v0.144.5 server-side compaction, verified on the wire (match ACTUAL phrasing).
            "context checkpoint compaction",
            "create a handoff summary",
            "another language model started to solve this problem",
            "produced a summary of its thinking",
        ),
        graft_compact_limit=True,
    ),
    "opencode": HarnessTui(
        name="opencode",
        model="stub",
        inflate=50_000,
        wire_path="/chat/completions",
        # composer-box text: "opencode" alone false-matches the sandbox NAME echoed to stdout.
        banner=("ask anything", "fix broken tests"),
        composer=("ask anything", "build ·"),
        # Auto-update modal may appear a couple seconds AFTER the composer; ESC=Skip. Broad
        # "update" marker so we ESC it whatever the exact title. NEVER Enter (confirms + EOFs).
        dialogs=(("update", "\x1b"),),
        summ_markers=(
            "create a new anchored summary from the conversation history",
            "output exactly the markdown structure",
            "anchored summary",
        ),
    ),
    "claurst": HarnessTui(
        name="claurst",
        model="ollama/stub",
        # 2M: far above any plausible window belief — claurst v0.1.6-danno1 does NOT auto-compact
        # even here (change-detector: compacts=False → C asserts summarization_requests == 0).
        inflate=2_000_000,
        wire_path="/chat/completions",
        banner=("claurst",),
        composer=("❯",),  # U+276F, NOT ascii '>'
        # First-run 2-page "Keyboard Shortcuts" onboarding overlay OVER the composer; ESC closes.
        dialogs=(("esc close", "\x1b"), ("keyboard shortcuts", "\x1b")),
        summ_markers=("concise yet thorough conversation summaries", "conversation summar"),
    ),
}


def settle_and_dismiss(driver: TuiDriver, cfg: HarnessTui, rounds: int = 6) -> None:
    """A fixed settle window: `rounds` × (pump ~2.5s → ESC/answer any on-screen first-run modal
    → pump toward the composer). A FIXED window (no early exit) on purpose — it must wait out
    opencode's LATE auto-update modal, which appears seconds AFTER the composer is already up.
    NEVER sends Enter while a dialog could be up (Enter confirms opencode's update, EOFing it)."""
    for _ in range(rounds):
        driver.pump(2.5)
        s = driver.screen().lower()
        hit = next((d for d in cfg.dialogs if d[0] in s), None)
        if hit:
            for ch in hit[1]:
                if not driver.send(ch):
                    break
                time.sleep(0.3)
            driver.pump(5, want=cfg.composer)


def submit(
    driver: TuiDriver,
    cfg: HarnessTui,
    wire: _WireLike,
    text: str,
    want: Sequence[str] | None = None,
    tries: int = 3,
) -> bool:
    """Land ONE turn on the wire: clear modals → type + Enter → CONFIRM a new request appeared
    on the capture proxy; retry if a racing overlay ate the Enter (mandatory for claurst's
    onboarding overlay). `text` must be '?'-free (claurst opens help on '?' and eats later
    prompts). Retrying may double the composer text, but the stub replies regardless. Returns
    whether a new request landed."""
    for _ in range(tries):
        settle_and_dismiss(driver, cfg, rounds=3)
        before = wire.requests()
        if not driver.send(text):
            return False
        time.sleep(0.6)
        driver.enter()
        driver.pump(30, want=want)
        if wire.requests() > before:
            return True
    return False


def one_shot_inflate(engine: type, magnitude: int) -> Callable[[], None]:
    """Monkeypatch `engine.next_reply` to report `prompt_tokens=magnitude` on the FIRST reply
    only, so the harness sees exactly one over-budget turn (→ one compaction decision), never
    the unbounded runaway. `engine` is `book_em_danno.stubai.script.ScriptEngine`. Returns a
    restore callable the caller MUST invoke in a `finally`."""
    orig = engine.next_reply
    state = {"n": 0}

    def inflated(self):  # type: ignore[no-untyped-def]
        r = orig(self)
        state["n"] += 1
        return dataclasses.replace(r, prompt_tokens=magnitude) if state["n"] == 1 else r

    engine.next_reply = inflated  # type: ignore[method-assign, assignment]

    def restore() -> None:
        engine.next_reply = orig  # type: ignore[method-assign]

    return restore
