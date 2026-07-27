"""The codex harness (a dialer installed post-provision).

codex (OpenAI's `@openai/codex` CLI) is not a prebuilt `docker sandbox` image: it runs in a
`shell` sandbox and installs via `npm install -g @openai/codex` post-provision. It speaks
ONLY the OpenAI Responses API and dials its endpoint RELAY-FREE through the egress proxy
(like claurst) — its per-turn `config.toml` (a custom provider → the dial base_url,
`wire_api = "responses"`) is written inline by `driver.codex_run`. It dials either local
Ollama (≥ 0.13.3 for `/v1/responses`, custom `ollama-danno` provider) or an OpenAI-compatible
cloud endpoint that offers Responses (custom `openai-danno` provider + injected key — Layer 3);
a Chat-only cloud endpoint is a loud N/A (the Responses-only `speaks` gates it). The turn/
install/matrix implementations live in `danno_validator.codex` (install + `TurnFn`) and
`driver.codex_run`; this module binds them into the registry value. See
`.docs/codex-integration.md` + `.docs/plan-harness-api.md` §5 Phase 3.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from book_em_danno.commands import sandbox as sb
from book_em_danno.config.schema import DannoConfig, OpenAIBackend
from book_em_danno.core.exec import Runner
from danno_validator import codex as _impl
from danno_validator.driver import CodexProvider
from danno_validator.harnesses import Harness, HarnessKind, WireProtocol, register
from danno_validator.harnesses._dialer import dialable_variants
from danno_validator.matrix import ConfigVariant

# Capability: codex speaks the OpenAI Responses API ONLY, and dials a local Ollama or an
# OpenAI-compatible cloud endpoint (Layer 3). The Responses-only `speaks` is what gates a
# Chat-only cloud endpoint (NVIDIA NIM/vLLM) out on its own even though its kind is in
# `dials` — see `_dialer.dialable_variants`. A model it can't speak is a harness-capability
# boundary, not a danno bug: `_model_matrix` drops it from an implicit sweep (loud N/A) and
# fails loud only when named explicitly via `--only`.
_CODEX_SPEAKS = frozenset({WireProtocol.RESPONSES})
_CODEX_DIALABLE_KINDS = frozenset({"ollama", "openai"})


def _install(runner: Runner, sandbox: str, config: DannoConfig | None = None) -> list[list[str]]:
    """Install the pinned codex CLI (npm, idempotent). `config` is unused: codex has a
    fixed install-time version."""
    return [_impl.install_codex(runner, sandbox)]


def _cloud_env_lines(config: DannoConfig, model_name: str) -> list[str]:
    return sb.codex_cloud_env_lines(config, model_name)


def _dial_ref(config: DannoConfig, variant: ConfigVariant) -> str | None:
    """The bare model tag codex dials (`-m <tag>`) for a variant — codex selects the model
    within its configured provider, so the `<provider>/` prefix is stripped."""
    return sb.resolve_codex_model(config, variant.model_name)


def _dial_provider(config: DannoConfig, variant: ConfigVariant) -> CodexProvider | None:
    """The CLOUD dial target for a variant, or None for a local Ollama row.

    An OpenAI-compatible backend → a `CodexProvider` carrying the backend base_url (already
    the recording-proxy URL under bench, since `config` is the capture-rewritten one; the
    original endpoint interactively), the key-env NAME, and the model's `reasoning_effort`.
    The Responses-vs-Chat gate lives in the matrix (`_model_matrix`), not here — by the time
    this runs the variant is known speakable, so this only assembles the dial target."""
    backend = config.backends[config.models[variant.model_name].backend]
    if isinstance(backend, OpenAIBackend):
        return CodexProvider(
            base_url=backend.base_url,
            env_key=backend.api_key_env,
            reasoning_effort=config.models[variant.model_name].reasoning_effort,
        )
    return None


def _resolve_start(target_abs: Path, value: str) -> tuple[str, list[str]]:
    return sb.resolve_codex_start(target_abs, "codex", value)


def _launch_argv(model: str | None, harness_args: list[str], capture_port: int | None) -> list[str]:
    """codex has no prebuilt binary launch: it runs via a `bash -lc` script that writes its
    `config.toml` inline (custom provider → host Ollama, or the `--capture` recording proxy)
    then opens the interactive TUI with the resolved `-m` ref."""
    return _impl.interactive_launch_script(model, harness_args, capture_port=capture_port)


def _model_matrix(config: DannoConfig, only: Sequence[str] | None) -> list[ConfigVariant]:
    return dialable_variants(
        config, only, speaks=_CODEX_SPEAKS, dials=_CODEX_DIALABLE_KINDS, harness="codex"
    )


def _provenance(config: DannoConfig) -> dict:
    return {"codex_version": _impl.CODEX_VERSION}  # the pinned npm dist


register(
    Harness(
        name="codex",
        kind=HarnessKind.DIALER,
        wire_protocol=WireProtocol.RESPONSES,
        # codex speaks the Responses API only; dials local Ollama + an OpenAI-compatible cloud
        # (a Chat-only cloud endpoint is gated out by the Responses-only `speaks`, not `dials`).
        speaks=_CODEX_SPEAKS,
        dials=_CODEX_DIALABLE_KINDS,
        sandbox_image=_impl.CODEX_SANDBOX_IMAGE,
        supports_capture=True,
        capture_via_relay=True,
        # No override surface yet: codex's config.toml is danno-owned + minimal (provider +
        # base_url), so it honors no `[<element>.overrides.codex]` — claiming the key would
        # accept config it silently ignores (Working Rule 8). Revisit when codex grows a
        # richer generated config (per-model options, agents).
        overrides_key=None,
        reap_patterns=("codex",),
        survivor_patterns=(r"[c]odex",),
        install=_install,
        env_lines=sb._codex_env_lines,
        launch_argv=_launch_argv,
        turn_fn=_impl.authed_codex_run,
        cloud_env_lines=_cloud_env_lines,
        dial_ref=_dial_ref,
        dial_provider=_dial_provider,
        model_matrix=_model_matrix,
        provenance=_provenance,
        resolve_start=_resolve_start,
        # No emit_config: codex's config.toml is written inline per session (self-contained,
        # matching the headless path) rather than emitted into a mounted HOME.
    )
)
