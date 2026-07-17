"""The occ harness (open-claude-code, a dialer installed post-provision).

occ runs in a `shell` sandbox with the fork cloned + built post-provision. It dials
local Ollama through an in-VM relay and reaches wired cloud providers via
`OPENAI_BASE_URL`/`OPENAI_API_KEY`; it needs a dial-ref override
(`resolve_occ_model`) for the same locality-check reason as claurst, and reads no
danno-generated config (`overrides_key=None`). The turn/install implementations
live in `danno_validator.occ` and `driver.occ_run`; this module binds them.

NOTE: occ is slated for removal (DoR `.docs/plan-harness-api.md`, Phase 2). Its
dropped-assistant-tool-call-turns finding is preserved in `.docs/` + memory; this
module and its impl are deleted together once the registry has landed.
"""

from __future__ import annotations

from collections.abc import Sequence

from book_em_danno.commands import sandbox as sb
from book_em_danno.config.schema import DannoConfig
from book_em_danno.core.exec import Runner
from danno_validator import occ as _impl
from danno_validator.harnesses import Harness, HarnessKind, WireProtocol, register
from danno_validator.harnesses._dialer import openai_compat_variants
from danno_validator.matrix import ConfigVariant


def _install(runner: Runner, sandbox: str, config: DannoConfig | None = None) -> None:
    """Clone + build the danno-pinned occ fork. `config` carries the [env] OCC_REPO/
    OCC_REF pins through to the installer."""
    _impl.install_occ(runner, sandbox, config)


def _cloud_env_lines(config: DannoConfig, model_name: str) -> list[str]:
    return sb.occ_cloud_env_lines(config, model_name)


def _dial_ref(config: DannoConfig, variant: ConfigVariant) -> str | None:
    return sb.resolve_occ_model(config, variant.model_name)


def _model_matrix(config: DannoConfig, only: Sequence[str] | None) -> list[ConfigVariant]:
    return openai_compat_variants(config, only)


def _provenance(config: DannoConfig) -> dict:
    repo, ref = _impl.occ_repo_ref(config)  # danno-owned repo + commit ref
    return {"occ_repo": repo, "occ_ref": ref}


register(
    Harness(
        name="occ",
        kind=HarnessKind.DIALER,
        wire_protocol=WireProtocol.CHAT,
        sandbox_image=_impl.OCC_SANDBOX_IMAGE,
        supports_capture=True,
        overrides_key=None,
        # occ's headless entry is a Node `index.mjs`; its local Ollama relay is
        # tagged `DANNO_RELAY`. The relay is a persistent in-VM helper, so it is
        # reaped but never counted as a turn "survivor" (hence not in survivors).
        reap_patterns=(r"index\.mjs", "DANNO_RELAY"),
        survivor_patterns=(r"[i]ndex\.mjs",),
        install=_install,
        turn_fn=_impl.authed_occ_run,
        cloud_env_lines=_cloud_env_lines,
        dial_ref=_dial_ref,
        model_matrix=_model_matrix,
        provenance=_provenance,
    )
)
