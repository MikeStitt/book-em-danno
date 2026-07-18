"""The claurst harness (a dialer installed post-provision).

claurst (a pure-Rust Claude-Code clone, danno-pinned fork build) is not a prebuilt
`docker sandbox` image: it runs in a `shell` sandbox and installs the release
binary post-provision. It dials local Ollama relay-free through the egress proxy
and reaches wired cloud providers directly; a cloud ref is authenticated by the
provider key under its own `api_key_env`. Because a matrix ref's backend segment
may not be the literal `ollama` its locality check expects, it dials a normalized
ref (`resolve_claurst_model`). The turn/install/matrix implementations live in
`danno_validator.claurst` (install + `TurnFn`) and `driver.claurst_run`; this
module binds them into the registry value.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from book_em_danno.commands import sandbox as sb
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import Runner
from danno_validator import claurst as _impl
from danno_validator.harnesses import Harness, HarnessKind, WireProtocol, register
from danno_validator.harnesses._dialer import openai_compat_variants
from danno_validator.matrix import ConfigVariant


def _install(runner: Runner, sandbox: str, config: DannoConfig | None = None) -> list[list[str]]:
    """Install the danno-pinned claurst binary (curl-fetched, idempotent). `config`
    is unused: claurst has a fixed install-time version."""
    return [_impl.install_claurst(runner, sandbox)]


def _cloud_env_lines(config: DannoConfig, model_name: str) -> list[str]:
    return sb.claurst_cloud_env_lines(config, model_name)


def _dial_ref(config: DannoConfig, variant: ConfigVariant) -> str | None:
    return sb.resolve_claurst_model(config, variant.model_name)


def _resolve_start(target_abs: Path, value: str) -> tuple[str, list[str]]:
    return sb.resolve_claurst_start(target_abs, "claurst", value)


def _launch_argv(model: str | None, harness_args: list[str], capture_port: int | None) -> list[str]:
    """claurst has no prebuilt binary launch: it runs via the relay-bracketed `bash -lc`
    launch script (mirrors the headless path), with the resolved `-m` ref and any
    `--capture` recording-proxy port folded in."""
    return _impl.interactive_launch_script(model, harness_args, capture_port=capture_port)


def _model_matrix(config: DannoConfig, only: Sequence[str] | None) -> list[ConfigVariant]:
    return openai_compat_variants(config, only)


def _provenance(config: DannoConfig) -> dict:
    return {"claurst_version": _impl.CLAURST_VERSION}  # danno-owned release tag


register(
    Harness(
        name="claurst",
        kind=HarnessKind.DIALER,
        wire_protocol=WireProtocol.CHAT,
        sandbox_image=_impl.CLAURST_SANDBOX_IMAGE,
        supports_capture=True,
        capture_via_relay=True,
        overrides_key="claurst",
        reap_patterns=("claurst",),
        survivor_patterns=(r"[c]laurst",),
        install=_install,
        env_lines=sb._claurst_env_lines,
        launch_argv=_launch_argv,
        turn_fn=_impl.authed_claurst_run,
        cloud_env_lines=_cloud_env_lines,
        dial_ref=_dial_ref,
        model_matrix=_model_matrix,
        provenance=_provenance,
        resolve_start=_resolve_start,
        emit_config=sb._emit_claurst_config,
    )
)
