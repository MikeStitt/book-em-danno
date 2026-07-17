"""The opencode harness (the default dialer).

opencode is a prebuilt `docker sandbox` image, so it has no post-provision install
step. It reads the danno-generated `.opencode/opencode.jsonc` (its provider +
model registry) and dials local Ollama / an OpenAI-compatible cloud through the
sandbox egress proxy, so it needs no per-turn relay and no dial-ref override (its
provider is the backend name in the generated config). Cloud auth is the provider
key under its own `api_key_env` name.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from book_em_danno.commands import sandbox as sb
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import Runner
from danno_validator.driver import Turn, TurnFn, opencode_run
from danno_validator.harnesses import Harness, HarnessKind, WireProtocol, register
from danno_validator.harnesses._dialer import openai_compat_variants
from danno_validator.matrix import ConfigVariant
from danno_validator.sweep import DEFAULT_RUN_AGENT


def _install(runner: Runner, sandbox: str, config: DannoConfig | None = None) -> None:
    """No-op: opencode is a prebuilt image with nothing to install post-provision."""


def _turn_fn(
    env_file: Path | None,
    *,
    capture_port: int | None = None,
    model_override: str | None = None,
    max_turns: int | None = None,
) -> TurnFn:
    """The `TurnFn` driving one opencode turn, with `env_file` bound.

    opencode is pinned to its read-write run-agent (`DEFAULT_RUN_AGENT`, "build")
    so benchmark edits actually land, and it captures via its rewritten backend
    `base_url` — so `capture_port`/`model_override`/`max_turns` are not used here.
    """

    def run(
        runner: Runner,
        name: str,
        prompt: str,
        *,
        session: str | None = None,
        agent: str | None = None,
        model: str | None = None,
        skip_permissions: bool = False,
        workspace: str | Path | None = None,
    ) -> Turn:
        return opencode_run(
            runner,
            name,
            prompt,
            session=session,
            agent=DEFAULT_RUN_AGENT,
            model=model,
            skip_permissions=skip_permissions,
            workspace=workspace,
            env_file=env_file,
        )

    return run


def _cloud_env_lines(config: DannoConfig, model_name: str) -> list[str]:
    return sb.cloud_api_key_env_lines(config, model_name)


def _dial_ref(config: DannoConfig, variant: ConfigVariant) -> str | None:
    return None  # opencode's provider IS the backend name in opencode.jsonc


def _model_matrix(config: DannoConfig, only: Sequence[str] | None) -> list[ConfigVariant]:
    return openai_compat_variants(config, only)


def _provenance(config: DannoConfig) -> dict:
    return {}  # image-provided: the prebuilt sandbox ships the binary; danno pins nothing


register(
    Harness(
        name="opencode",
        kind=HarnessKind.DIALER,
        wire_protocol=WireProtocol.CHAT,
        sandbox_image="opencode",
        supports_capture=True,
        overrides_key="opencode",
        reap_patterns=("opencode",),
        survivor_patterns=(r"[o]pencode",),
        install=_install,
        turn_fn=_turn_fn,
        cloud_env_lines=_cloud_env_lines,
        dial_ref=_dial_ref,
        model_matrix=_model_matrix,
        provenance=_provenance,
    )
)
