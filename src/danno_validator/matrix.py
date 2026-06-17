"""Expand one danno.toml into the set of configurations to sweep.

The matrix generator is the entry to M2: from a single base `DannoConfig` it
produces N `ConfigVariant`s, each pinning one axis of variation. The first (and
default) axis is **model** — the central question this harness answers is *which
declared models actually work* — so `model_variants` yields one variant per model
the base config declares, each driving the Level-0 battery with that model via
OpenCode's `-m <provider/tag>` ref.

It is pure (no I/O): it reuses `book_em_danno.config` for both the schema and the
`model_ref` resolver, so a variant's ref is exactly what `render_config` would
emit and what OpenCode expects. The sweep (`sweep.py`) is what turns these
variants into provisioned runs; keeping expansion separate keeps it unit-testable
without a sandbox.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from book_em_danno.config.generate import model_ref
from book_em_danno.config.schema import DannoConfig


@dataclass(frozen=True)
class ConfigVariant:
    """One point in the configuration matrix: a model to drive the battery with.

    `model_name` is the danno.toml model key; `model_ref` is the resolved OpenCode
    `-m` reference (e.g. `ollama/gemma3:27b`). `description` is a human label for
    the report. M2 varies only the model; the dataclass carries the danno key so
    later axes (prompts/tools) can attach to the same variant identity.
    """

    model_name: str
    model_ref: str
    description: str


def model_variants(
    config: DannoConfig, *, only: Sequence[str] | None = None
) -> list[ConfigVariant]:
    """One `ConfigVariant` per model declared in `config`, sorted by danno key.

    `only` restricts the sweep to the named models (fail loud, Working Rule 8, on
    a name the config doesn't declare). The whole declared catalog is the default
    candidate set — sweeping every model is the point, not just agent-assigned
    ones. `model_ref` resolution raises for an unimplemented backend (llamacpp) or
    a model missing its `tag`/`id`, surfacing a broken base config up front rather
    than mid-sweep.
    """
    names = sorted(config.models)
    if only is not None:
        unknown = [n for n in only if n not in config.models]
        if unknown:
            raise ValueError(
                f"matrix `only` names models not declared in danno.toml: {', '.join(unknown)}. "
                f"Declared: {', '.join(names) or '(none)'}."
            )
        names = [n for n in names if n in set(only)]
    return [
        ConfigVariant(
            model_name=name,
            model_ref=(ref := model_ref(config, name)),
            description=ref,
        )
        for name in names
    ]
