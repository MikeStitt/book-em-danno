"""A formal Harness contract + a self-discovering registry.

A *harness* is the outer coding tool danno drives (opencode / claurst / claude,
and — added later — codex). Historically each harness was a bare `str` threaded
through `if harness == …` chains scattered across install, config-gen, env,
model-resolution, cloud-auth, capture, launch, telemetry, and matrix membership.
This package replaces that with **one value object per harness** plus a registry,
so the rest of the system self-discovers the registered harnesses instead of
hardcoding the name-set and branching on it. Adding a harness = a new module in
this package + one import line at the bottom of this file (see the DoR
`.docs/plan-harness-api.md`).

Two harness *kinds* share one contract:

- **Dialer** — danno points it at an endpoint it controls (local Ollama or an
  OpenAI-compatible cloud): opencode, claurst, codex. They share the model
  matrix, cloud-auth, dial-ref, and capture machinery.
- **Reference** — carries its own endpoint + auth and selects by native
  `--model` over inert-backend models: claude. A registered harness (so dispatch
  stays uniform) that declares `kind = REFERENCE` and implements a partial
  contract (no danno-dialed endpoint, no capture).

The per-turn transcript stays the existing `Turn`/`TurnFn` seam in `driver.py`;
this package owns everything *around* a turn that used to be a name branch.

Home is `danno_validator` (not `book_em_danno`) because `danno_validator` already
depends on `book_em_danno`, and `book_em_danno` only ever reaches back via
deferred local imports to avoid the cycle — a direction this registry preserves.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from book_em_danno.config.schema import DannoConfig
    from book_em_danno.core.exec import Runner
    from danno_validator.driver import TurnFn
    from danno_validator.matrix import ConfigVariant


class WireProtocol(StrEnum):
    """The request/response wire shape a harness speaks — routes capture, wire
    metrics, and the #97 history-well-formedness assertion."""

    CHAT = "chat"  # OpenAI chat-completions: opencode (/v1 openai), claurst (relay)
    RESPONSES = "responses"  # OpenAI Responses API: codex
    ANTHROPIC = "anthropic"  # claude → api.anthropic.com


class HarnessKind(StrEnum):
    DIALER = "dialer"  # danno dials an endpoint it controls (opencode/claurst/codex)
    REFERENCE = "reference"  # carries its own endpoint+auth, selects by --model (claude)


@dataclass(frozen=True)
class Harness:
    """Everything unique to one harness, as a value object (not a god-class).

    Value fields describe the harness; the `Callable` fields are the seams the
    provisioning (`book_em_danno`) and driving (`danno_validator`) layers call
    instead of branching on the name. A `REFERENCE` harness fills the
    dialer-only seams with meaningful no-ops (e.g. `dial_ref` → None,
    `cloud_env_lines` → []), guarded at call sites by `kind`/`supports_capture`.
    """

    name: str
    kind: HarnessKind
    wire_protocol: WireProtocol
    # The prebuilt `docker sandbox create <image>` image to provision. "shell" for
    # harnesses installed post-provision (claurst); otherwise the harness's own name.
    sandbox_image: str
    # Whether `--capture` can record this harness's wire traffic (dialers: yes;
    # the cloud claude reference row talks straight to api.anthropic.com: no).
    supports_capture: bool
    # The `[<name>.overrides.<harness>]` escape-hatch key in `config/schema.py`,
    # or None for harnesses that read no danno-generated config (claude/occ).
    overrides_key: str | None
    # Process-name fragments for the post-runaway-kill VM reap (`suites/base.py`).
    # `reap_patterns` is the full set (incl. persistent in-VM helpers like the occ
    # relay); `survivor_patterns` is the bracketed subset used for the survivor
    # probe (excludes persistent helpers, which are never a turn "survivor").
    reap_patterns: tuple[str, ...]
    survivor_patterns: tuple[str, ...]

    # --- provisioning seam (book_em_danno) -----------------------------------
    # Post-provision install for HUTs that aren't a prebuilt image (claurst).
    # No-op for prebuilt-image harnesses (opencode/claude).
    install: Callable[[Runner, str, DannoConfig | None], None]

    # --- driving seam (danno_validator) --------------------------------------
    # Factory returning the `TurnFn` for one turn, with the auth `env_file` bound.
    # `capture_port`/`model_override`/`max_turns` are honored by the harnesses that
    # use them and ignored by the rest.
    turn_fn: Callable[..., TurnFn]

    # --- model / cloud resolution --------------------------------------------
    # Cloud-provider auth env-file lines for one variant, or [] for local/reference.
    cloud_env_lines: Callable[[DannoConfig, str], list[str]]
    # The ref this harness must actually dial for a variant, or None (report ref stands).
    dial_ref: Callable[[DannoConfig, ConfigVariant], str | None]
    # The model matrix to sweep for this harness (dialer: OpenAI-compatible catalog
    # minus inert; reference: inert models, or a single baseline row).
    model_matrix: Callable[[DannoConfig, Sequence[str] | None], list[ConfigVariant]]

    # --- telemetry -----------------------------------------------------------
    # danno-owned version pins for this harness (merged into `harness_provenance`).
    provenance: Callable[[DannoConfig], dict]


_REGISTRY: dict[str, Harness] = {}


def register(h: Harness) -> Harness:
    """Register a harness, failing loud on a duplicate name (Working Rule 8)."""
    if h.name in _REGISTRY:
        raise ValueError(f"duplicate harness '{h.name}'")
    _REGISTRY[h.name] = h
    return h


def get(name: str) -> Harness:
    """The registered harness, or a loud error naming the valid set."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(
            f"unknown harness '{name}'. Valid harnesses: {', '.join(all_names())}."
        ) from None


def all_names() -> tuple[str, ...]:
    """The registered harness names, in registration (report-column) order."""
    return tuple(_REGISTRY)


# Import each submodule so importing the package self-populates the registry. Adding
# a harness = a new module here + one name on this line. This import order is the
# registration order, which sets the matrix/report column layout — keep it stable
# (opencode, claurst, occ, claude), matching the former `BENCH_HARNESSES` tuple.
from danno_validator.harnesses import opencode as opencode  # noqa: E402,F401,I001
from danno_validator.harnesses import claurst as claurst  # noqa: E402,F401
from danno_validator.harnesses import occ as occ  # noqa: E402,F401
from danno_validator.harnesses import claude as claude  # noqa: E402,F401
