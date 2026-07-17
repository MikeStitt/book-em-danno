"""Model-matrix logic shared by every *dialer* harness (opencode/claurst/codex).

A dialer's matrix is the declared danno.toml catalog **minus** inert-backend
models: an inert model's `tag` is a native `--model` alias (the claude reference
row's lever), not an endpoint danno can dial, so a dialer would raise loud trying
to reach one mid-sweep. This is the counterpart of `claude.reference_matrix`,
which keeps ONLY inert models. Moved here (from `suites/bench.py`) so it lives on
the harness side and every dialer binds the same function.
"""

from __future__ import annotations

from collections.abc import Sequence

from book_em_danno.config.schema import DannoConfig, InertBackend
from danno_validator.matrix import ConfigVariant, model_variants


def openai_compat_variants(config: DannoConfig, only: Sequence[str] | None) -> list[ConfigVariant]:
    """The dialer model matrix: the declared catalog minus inert-backend models.

    An explicit `--only` naming an inert model for a dialer is an impossible
    pairing (the model can't be dialed) — fail loud (Working Rule 8) naming it,
    rather than silently dropping it to an empty sweep.
    """
    variants = model_variants(config, only=only)
    inert = {
        v.model_name
        for v in variants
        if isinstance(config.backends[config.models[v.model_name].backend], InertBackend)
    }
    if only is not None and inert:
        raise ValueError(
            f"models {sorted(inert)} are on an inert backend (the claude reference row only) "
            f"and can't be dialed by an OpenAI-compatible harness (opencode/claurst). "
            f"Drop them from --only, or run them with --harness claude."
        )
    return [v for v in variants if v.model_name not in inert]
