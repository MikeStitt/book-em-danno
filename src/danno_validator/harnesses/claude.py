"""The claude harness (the registered-but-unique *reference* harness).

claude carries its own endpoint (api.anthropic.com) and auth (a host
`CLAUDE_CODE_OAUTH_TOKEN`/`ANTHROPIC_API_KEY`, built into a chmod-600 env-file by
`baseline`), and selects its model by native `--model` over **inert-backend**
models — not the OpenAI-compatible `-m` matrix the dialers sweep. It is registered
so dispatch stays uniform, but declares `kind = REFERENCE` and fills the
dialer-only seams with reference semantics: no cloud auth injection (it carries
its own), no dial-ref override, no capture, no danno-owned version pin. The turn
implementation lives in `driver.claude_run` / `baseline`; this module binds it.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from book_em_danno.commands import sandbox as sb
from book_em_danno.config.schema import DannoConfig, InertBackend
from book_em_danno.core.exec import Runner
from danno_validator import baseline
from danno_validator.driver import TurnFn
from danno_validator.harnesses import Harness, HarnessKind, WireProtocol, register
from danno_validator.matrix import ConfigVariant, model_variants


def _install(runner: Runner, sandbox: str, config: DannoConfig | None = None) -> list[list[str]]:
    """No-op: claude is a prebuilt image with nothing to install post-provision."""
    return []


def _turn_fn(
    env_file: Path | None,
    *,
    capture_port: int | None = None,
    model_override: str | None = None,
    max_turns: int | None = None,
) -> TurnFn:
    """The `TurnFn` driving one claude turn. `env_file` carries auth (never None —
    bench/sweep build it from a host token before dispatch); `model_override` is the
    `--model` value (an inert model's tag) or None → claude's install default.
    `capture_port`/`max_turns` are meaningless for the cloud reference row."""
    if env_file is None:  # defensive: the caller builds the auth file before dispatch
        raise ValueError("claude HUT requires an auth env-file (host token)")
    return baseline._authed_claude_run(env_file, model_override)


def _launch_argv(model: str | None, harness_args: list[str], capture_port: int | None) -> list[str]:
    """claude is a prebuilt binary: launch it directly with any passthrough args (e.g.
    `--model <x>` / `--resume <id>`). `model`/`capture_port` are unused here — the
    reference row selects its model via the flags forwarded in `harness_args`."""
    return ["claude", *harness_args]


def _cloud_env_lines(config: DannoConfig, model_name: str) -> list[str]:
    return []  # the reference HUT carries its own auth (never a [models] cloud key)


def _dial_ref(config: DannoConfig, variant: ConfigVariant) -> str | None:
    """claude's `--model` value for a variant: an INERT-backend model's tag (e.g.
    "claude-opus-4-8"), or None (the synthetic baseline row / a non-inert model) →
    claude's install default."""
    model = config.models.get(variant.model_name)
    if model is not None and isinstance(config.backends[model.backend], InertBackend):
        return model.tag
    return None


def inert_model_names(config: DannoConfig, only: Sequence[str] | None) -> list[str]:
    """The declared inert-backend model names claude should sweep (sorted, `only`-filtered).

    Public so `suites/bench.py` can re-export it as its `_claude_inert_models` seam
    without duplicating the InertBackend scan.
    """
    names = [
        n
        for n in sorted(config.models)
        if isinstance(config.backends[config.models[n].backend], InertBackend)
    ]
    if only is not None:
        keep = set(only)
        names = [n for n in names if n in keep]
    return names


def _model_matrix(config: DannoConfig, only: Sequence[str] | None) -> list[ConfigVariant]:
    """claude's matrix: its declared inert-backend models (each `tag` → `--model`), or
    a single install-default `claude-code` reference row when none are declared."""
    inert = inert_model_names(config, only)
    if inert:
        return model_variants(config, only=inert)
    return [baseline.baseline_variant(None)]


def _provenance(config: DannoConfig) -> dict:
    return {}  # image-provided: the prebuilt sandbox ships the binary; danno pins nothing


register(
    Harness(
        name="claude",
        kind=HarnessKind.REFERENCE,
        wire_protocol=WireProtocol.ANTHROPIC,
        # The reference row talks straight to api.anthropic.com and selects an inert model by
        # native `--model` (its `reference_matrix` keeps ONLY inert models — the mirror of a
        # dialer dropping them).
        speaks=frozenset({WireProtocol.ANTHROPIC}),
        dials=frozenset({"inert"}),
        sandbox_image="claude",
        supports_capture=False,
        overrides_key=None,
        reap_patterns=(),  # claude runs in its own sandbox; not part of the HUT reap
        survivor_patterns=(),
        install=_install,
        env_lines=sb._claude_env_lines,
        launch_argv=_launch_argv,
        turn_fn=_turn_fn,
        cloud_env_lines=_cloud_env_lines,
        dial_ref=_dial_ref,
        model_matrix=_model_matrix,
        provenance=_provenance,
        pre_session=sb.seed_onboarding,
        update_advice=sb._claude_update_advice,
    )
)
