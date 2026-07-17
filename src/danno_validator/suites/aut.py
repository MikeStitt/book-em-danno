"""Harness-under-test resolution shared by the sweep and the benchmark runner.

Maps a `--harness` name to (1) the prebuilt sandbox image to provision, (2) the
post-provision install step (claurst is not a prebuilt image), and (3) the `TurnFn`
that drives a turn. These are now thin lookups into the harness registry
(`danno_validator.harnesses`), so `danno bench` and `danno validate` resolve the
HUT identically and adding a harness needs no edit here — the registry owns the
per-harness behavior (see `.docs/plan-harness-api.md`).
"""

from __future__ import annotations

from pathlib import Path

from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import Runner
from danno_validator import harnesses
from danno_validator.driver import TurnFn


def resolve_image(harness: str) -> str:
    """The prebuilt sandbox image to `docker sandbox create` for this HUT."""
    return harnesses.get(harness).sandbox_image


def install_harness(
    runner: Runner, sandbox: str, harness: str, config: DannoConfig | None = None
) -> None:
    """Post-provision install for HUTs that aren't a prebuilt image (claurst).

    `config` carries the `[env]` pins through to the installer; harnesses with a
    fixed install-time version (claurst) or nothing to install (opencode/claude,
    prebuilt images) ignore it / no-op.
    """
    harnesses.get(harness).install(runner, sandbox, config)


def run_turn_for(
    harness: str,
    env_file: Path | None,
    capture_port: int | None = None,
    model_override: str | None = None,
    max_turns: int | None = None,
) -> TurnFn:
    """The `TurnFn` driving one turn for this HUT, with `env_file` bound.

    claurst sets up its Ollama relay per turn; opencode is pinned to its read-write
    run-agent ("build") so benchmark edits land. claude is the cloud *reference* HUT —
    its `env_file` carries auth (never None; built loud from a host token) and it
    selects its model via `--model` (`model_override`). `capture_port` (from
    `--capture`) points a dialer's in-VM Ollama relay at the recording proxy;
    `model_override` is the harness's own model selector (`Harness.dial_ref`): for a
    dialer the normalized ref it dials, for claude the `--model` value; None → the
    harness default. Harnesses that don't use a given argument ignore it.
    """
    return harnesses.get(harness).turn_fn(
        env_file,
        capture_port=capture_port,
        model_override=model_override,
        max_turns=max_turns,
    )
