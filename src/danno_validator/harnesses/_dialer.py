"""Model-matrix logic shared by every *dialer* harness (opencode/claurst/codex).

A dialer's matrix is the declared danno.toml catalog restricted to the cells this
harness can actually *speak* — the "speakable" predicate, evaluated per model:

    backend.kind ∈ harness.dials                          (danno can dial the endpoint)
    ∧ (harness.speaks ∩ backend.offers) ≠ ∅               (they share a wire protocol)
    ∧ model.requires_wire ⊆ (harness.speaks ∩ backend.offers)   (the model's need is met)

`dials`/`speaks` come from the harness registry value; `backend.offers`
(`schema.backend_wire_offers`) and `model.requires_wire` from danno.toml. An inert
model fails the FIRST clause (a dialer never lists `"inert"` in `dials`), so the
inert-drop opencode/claurst have always done is just this predicate's `kind ∉ dials`
case — the counterpart of `claude.reference_matrix`, which keeps ONLY inert models.
The protocol clauses split `kind="openai"` into Chat-only hosts (NVIDIA NIM/vLLM) vs
dual-protocol ones (api.openai.com): codex (speaks Responses only) is N/A for a
Chat-only cloud endpoint even once its `dials` includes `"openai"`.
"""

from __future__ import annotations

from collections.abc import Sequence

from book_em_danno.config.schema import DannoConfig, backend_wire_offers
from book_em_danno.core.exec import log_warn
from danno_validator.matrix import ConfigVariant, model_variants


def dialable_variants(
    config: DannoConfig,
    only: Sequence[str] | None,
    *,
    speaks: frozenset[str],
    dials: frozenset[str],
    harness: str,
) -> list[ConfigVariant]:
    """The dialer matrix restricted to the cells `harness` can speak (the predicate above).

    A model that fails the predicate is not a danno bug but a harness-capability boundary,
    handled the way the design settled on:

    - **Implicit sweep** (no `--only`): the unspeakable model is DROPPED, but LOUDLY — a
      `log_warn` names it and the reason, so the comparison grid reflects that this harness
      simply doesn't cover the cell (never a silent gap).
    - **Explicit `--only` naming it**: an impossible pairing — fail loud (Working Rule 8)
      rather than collapsing to a smaller-than-asked sweep or a fake-green empty run.

    Every dialer binds this with its own `speaks`/`dials` (opencode speaks {chat,responses}
    dials {ollama,openai}; claurst speaks {chat}; codex speaks {responses} dials {ollama})
    instead of branching on the harness name.
    """
    speaks = frozenset(str(s) for s in speaks)
    variants = model_variants(config, only=only)

    def _na_reason(v: ConfigVariant) -> str | None:
        """None if the harness can speak this model, else a one-line N/A reason."""
        backend = config.backends[config.models[v.model_name].backend]
        if backend.kind not in dials:
            return f"{v.model_name}: backend kind '{backend.kind}' not dialable"
        offers = backend_wire_offers(backend)
        shared = speaks & offers
        if not shared:
            return (
                f"{v.model_name}: no shared wire protocol "
                f"(harness speaks {sorted(speaks)}, backend offers {sorted(offers)})"
            )
        requires = config.models[v.model_name].requires_wire
        if requires and not (frozenset(requires) <= shared):
            return f"{v.model_name}: needs wire {sorted(requires)} but only {sorted(shared)} shared"
        return None

    na = [(v, r) for v in variants if (r := _na_reason(v)) is not None]
    if na:
        # No square brackets around the dynamic parts: `log_warn` renders via rich, which
        # would eat `[...]` as markup and swallow the very model names this must surface.
        detail = "; ".join(r for _, r in na)
        if only is not None:
            raise ValueError(
                f"harness '{harness}' can't speak these models: {detail}. Drop them from --only, "
                f"or run them with a harness that can (e.g. --harness opencode for a cloud Chat "
                f"row, --harness claude for an inert row)."
            )
        log_warn(
            f"harness '{harness}' is N/A for these models and skips them "
            f"(a harness-capability boundary, not a danno error): {detail}."
        )
    na_names = {v.model_name for v, _ in na}
    return [v for v in variants if v.model_name not in na_names]
