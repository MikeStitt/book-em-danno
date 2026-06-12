"""Pydantic models mirroring danno.toml — the declarative source of truth.

Validation lives at this boundary (Working Rule 7/8): unknown keys and dangling
references fail loud rather than producing a subtly wrong opencode.jsonc.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = "."


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_agent: str = "pm"
    profile: Literal["hybrid", "cloud-only", "local-only"] = "hybrid"


class OllamaBackend(BaseModel):
    """Local models via OpenCode's @ai-sdk/openai-compatible provider. IMPLEMENTED."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["ollama"]
    base_url: str
    num_ctx: int = 32000


class CloudBackend(BaseModel):
    """A cloud provider configured in OpenCode; keys stay in the env. IMPLEMENTED."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["cloud"]
    provider: str


class LlamacppBackend(BaseModel):
    """Local models via llama.cpp's OpenAI-compatible llama-server. STUBBED.

    The schema slot exists so danno.toml can declare it, but the generator raises
    a clear "not yet implemented" until the backend is built.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["llamacpp"]
    base_url: str


Backend = Annotated[
    OllamaBackend | CloudBackend | LlamacppBackend,
    Field(discriminator="kind"),
]


class Model(BaseModel):
    """A named (backend, tag/id) pair. `tag` for ollama/llamacpp, `id` for cloud."""

    model_config = ConfigDict(extra="forbid")
    backend: str
    tag: str | None = None
    id: str | None = None
    tool_call: bool = False


class Tool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    source: str
    install_to: Literal["sandbox", "project"]


_AGENT_HOME_KEYWORDS = frozenset({"per-project", "per-repo", "shared", "ephemeral"})


class Sandbox(BaseModel):
    """The `[sandbox]` block. `agent_home` is an identity key (see SAMPLE_README):
    a keyword, `group:<name>`, or an explicit host path. Sandboxes whose key
    resolves to the same path share one agent home."""

    model_config = ConfigDict(extra="forbid")
    agent_home: str = "per-project"

    @field_validator("agent_home")
    @classmethod
    def _check_agent_home(cls, v: str) -> str:
        if v in _AGENT_HOME_KEYWORDS:
            return v
        if v.startswith("group:") and len(v) > len("group:"):
            return v
        if v.startswith(("/", "~", ".")) or "/" in v:
            return v
        raise ValueError(
            f"invalid agent_home {v!r}: expected one of {sorted(_AGENT_HOME_KEYWORDS)}, "
            "'group:<name>', or a host path"
        )


class DannoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project: Project = Field(default_factory=Project)
    defaults: Defaults = Field(default_factory=Defaults)
    backends: dict[str, Backend] = Field(default_factory=dict)
    models: dict[str, Model] = Field(default_factory=dict)
    agents: dict[str, str] = Field(default_factory=dict)
    tools: list[Tool] = Field(default_factory=list)
    sandbox: Sandbox = Field(default_factory=Sandbox)

    @model_validator(mode="after")
    def _check_references(self) -> DannoConfig:
        for model_name, model in self.models.items():
            if model.backend not in self.backends:
                raise ValueError(
                    f"model '{model_name}' references unknown backend '{model.backend}'"
                )
        for agent, model_name in self.agents.items():
            if model_name not in self.models:
                raise ValueError(f"agent '{agent}' references unknown model '{model_name}'")
        return self
