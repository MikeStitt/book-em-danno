"""The codex harness (a dialer installed post-provision).

codex (OpenAI's `@openai/codex` CLI) is not a prebuilt `docker sandbox` image: it runs in a
`shell` sandbox and installs via `npm install -g @openai/codex` post-provision. It speaks
ONLY the OpenAI Responses API and dials local Ollama's `/v1/responses` endpoint RELAY-FREE
through the egress proxy (like claurst) — its per-turn `config.toml` (custom `ollama-danno`
provider → host Ollama, `wire_api = "responses"`) is written inline by `driver.codex_run`.
Phase-0 scope is local Ollama (≥ 0.13.3 for `/v1/responses`); a cloud codex row is not yet
wired (`sandbox.codex_cloud_env_lines` fails loud for a non-Ollama backend). The turn/install/
matrix implementations live in `danno_validator.codex` (install + `TurnFn`) and
`driver.codex_run`; this module binds them into the registry value. See
`.docs/codex-integration.md` + `.docs/plan-harness-api.md` §5 Phase 3.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from book_em_danno.commands import sandbox as sb
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import Runner
from danno_validator import codex as _impl
from danno_validator.harnesses import Harness, HarnessKind, WireProtocol, register
from danno_validator.harnesses._dialer import openai_compat_variants
from danno_validator.matrix import ConfigVariant


def _install(runner: Runner, sandbox: str, config: DannoConfig | None = None) -> list[list[str]]:
    """Install the pinned codex CLI (npm, idempotent). `config` is unused: codex has a
    fixed install-time version."""
    return [_impl.install_codex(runner, sandbox)]


def _cloud_env_lines(config: DannoConfig, model_name: str) -> list[str]:
    return sb.codex_cloud_env_lines(config, model_name)


def _dial_ref(config: DannoConfig, variant: ConfigVariant) -> str | None:
    """The bare model tag codex dials (`-m <tag>`) for a variant — codex selects the model
    within its configured provider, so the `<provider>/` prefix is stripped (Phase-0)."""
    return sb.resolve_codex_model(config, variant.model_name)


def _resolve_start(target_abs: Path, value: str) -> tuple[str, list[str]]:
    return sb.resolve_codex_start(target_abs, "codex", value)


def _launch_argv(model: str | None, harness_args: list[str], capture_port: int | None) -> list[str]:
    """codex has no prebuilt binary launch: it runs via a `bash -lc` script that writes its
    `config.toml` inline (custom provider → host Ollama, or the `--capture` recording proxy)
    then opens the interactive TUI with the resolved `-m` ref."""
    return _impl.interactive_launch_script(model, harness_args, capture_port=capture_port)


def _model_matrix(config: DannoConfig, only: Sequence[str] | None) -> list[ConfigVariant]:
    return openai_compat_variants(config, only)


def _provenance(config: DannoConfig) -> dict:
    return {"codex_version": _impl.CODEX_VERSION}  # the pinned npm dist


register(
    Harness(
        name="codex",
        kind=HarnessKind.DIALER,
        wire_protocol=WireProtocol.RESPONSES,
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
        model_matrix=_model_matrix,
        provenance=_provenance,
        resolve_start=_resolve_start,
        # No emit_config: codex's config.toml is written inline per session (self-contained,
        # matching the headless path) rather than emitted into a mounted HOME.
    )
)
