"""Pydantic models mirroring danno.toml — the declarative source of truth.

Validation lives at this boundary (Working Rule 7/8): unknown keys and dangling
references fail loud rather than producing a subtly wrong opencode.jsonc.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = "."


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_agent: str = "pm"
    profile: Literal["hybrid", "cloud-only", "local-only"] = "hybrid"


class OllamaBackend(BaseModel):
    """Local models via OpenCode's @ai-sdk/openai-compatible provider. IMPLEMENTED.

    Field semantics (see README "Ollama context & runtime knobs"). Note what is
    deliberately absent: there is NO knob here for Ollama's REAL context window or
    for streaming/thinking. Under the OpenAI-compatible `/v1` API a body `num_ctx`
    is ignored — Ollama loads the model at its FULL context — and opencode always
    streams (it hardcodes `stream: true`). The real window / RAM lever is an Ollama
    Modelfile variant, out of scope here. Reasoning is per-model (see Model).

      context_budget -> OpenCode's CLIENT-SIDE belief of the window
                        (models.<tag>.limit.context); used to trim/compact the
                        conversation. It does NOT change what Ollama loads.
      output_limit   -> tokens OpenCode reserves for the reply
                        (models.<tag>.limit.output); usable input ≈ context_budget
                        - output_limit.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["ollama"]
    base_url: str
    context_budget: int = 32000
    output_limit: int = 8192


class LlamacppBackend(BaseModel):
    """Local models via llama.cpp's OpenAI-compatible llama-server. STUBBED.

    The schema slot exists so danno.toml can declare it, but the generator raises
    a clear "not yet implemented" until the backend is built.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["llamacpp"]
    base_url: str


class OpenAIBackend(BaseModel):
    """A generic OpenAI-compatible endpoint (NVIDIA NIM, vLLM, OpenAI itself, …) via
    OpenCode's @ai-sdk/openai-compatible provider. IMPLEMENTED.

    Unlike `ollama` (no auth) this needs a key — but the secret is NEVER written
    here. `api_key_env` names an environment variable; the generator emits
    `apiKey: "{env:<api_key_env>}"`, and the value is injected at launch via
    `danno sandbox start --env <api_key_env>=…` (so it lands only in the chmod-600
    env-file, never in danno.toml or the committed opencode.jsonc). `context_budget`/
    `output_limit` map to `limit.context`/`limit.output` as for ollama."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["openai"]
    base_url: str
    api_key_env: str
    context_budget: int = 32000
    output_limit: int = 8192


class InertBackend(BaseModel):
    """A backend danno does NOT dial — the harness serves the model itself. IMPLEMENTED.

    For a reference harness that carries its own endpoint + auth and picks the model
    by a native flag rather than an OpenAI-compatible base_url danno controls. Today
    that is the `claude` reference row: Claude Code talks straight to
    api.anthropic.com with its own OAuth token and selects the model via `--model`,
    so danno has no base_url/api_key lever to emit — hence no fields here.

    A model on an inert backend uses its `tag` as the raw harness model id/alias
    (e.g. "claude-opus-4-8", "sonnet"): `model_ref` returns the bare tag (no
    `<backend>/` prefix), and `danno bench --harness claude` passes it to `--model`.
    Dialing an inert model with a config-driven harness (opencode/occ/claurst) fails
    loud, since there is no endpoint to reach.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["inert"]


Backend = Annotated[
    OllamaBackend | LlamacppBackend | OpenAIBackend | InertBackend,
    Field(discriminator="kind"),
]


class Model(BaseModel):
    """A named (backend, tag) pair. `tag` is the model id on the backend.

    `reasoning_effort` (ollama only) is emitted as the model-level camelCase
    `options.reasoningEffort`, which @ai-sdk/openai-compatible spreads raw into the
    `/v1` request body where Ollama honors it. "none" disables the thinking trace
    (faster, and avoids the opencode #21903 reasoning-field hang); leave unset to
    forward nothing. Note: gpt-oss-style models reject "none" — use low/medium/high
    for those (documented here, not validated, since the model id isn't known)."""

    model_config = ConfigDict(extra="forbid")
    backend: str
    tag: str | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None


class AgentSpec(BaseModel):
    """The rich `[agents.<name>]` form: danno's model lever plus the safe OpenCode
    agent pass-through fields, emitted verbatim into the generated opencode.jsonc
    `agent.<name>` block. The string shorthand (`agent = "model"`) covers the common
    case; this table form unlocks (1) routing a built-in subagent to a local model
    and (2) fully defining a danno-owned agent in JSON (no markdown needed).

    `model` resolves by the same '/'-rule as the shorthand (a value with '/' is a raw
    OpenCode ref, else a [models] name). The remaining fields mirror OpenCode's JSON
    agent schema and are emitted as-is. `prompt`/`tools`/`mode` etc. are OpenCode's,
    and where a markdown agent def already sets a field, OpenCode's MARKDOWN WINS over
    our JSON (verified) — the generator warns loud at that collision rather than
    emitting a value that will be silently ignored."""

    model_config = ConfigDict(extra="forbid")
    model: str | None = None
    mode: Literal["primary", "subagent", "all"] | None = None
    description: str | None = None
    prompt: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    steps: int | None = None
    disable: bool | None = None
    hidden: bool | None = None
    color: str | None = None
    permission: dict[str, Any] | None = None


class Tool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    source: str
    install_to: Literal["sandbox", "project"]


class NpmPlugin(BaseModel):
    """An OpenCode npm plugin, declared in opencode.jsonc's `"plugin"` array and
    auto-installed by OpenCode (Bun) in the sandbox at startup. Unlike `[[tools]]`
    (imperative installs like ADOS), these are declarative config.

    `config` (when set) renders as the documented `[package, config]` tuple form.
    `setup` is an optional list of in-container shell commands run post-create via
    `docker sandbox exec` (e.g. a plugin's slash-command installer)."""

    model_config = ConfigDict(extra="forbid")
    package: str
    config: dict[str, Any] | None = None
    setup: list[str] = Field(default_factory=list)

    @field_validator("package")
    @classmethod
    def _check_package(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("npm plugin 'package' must be non-empty")
        return v


_AGENT_HOME_KEYWORDS = frozenset({"per-project", "per-repo", "shared", "ephemeral"})


class Sandbox(BaseModel):
    """The `[sandbox]` block. `agent_home` is an identity key (see README "Sandboxed agents"):
    a keyword, `group:<name>`, or an explicit host path. Sandboxes whose key
    resolves to the same path share one agent home."""

    model_config = ConfigDict(extra="forbid")
    agent_home: str = "per-project"
    # SBX-TRANSITION(docker-sandbox-deprecation): which sandbox CLI to drive.
    # "auto" prefers `sbx` when installed, else the deprecated `docker sandbox`.
    # The "docker" branch is transition support — REMOVE it and this option once
    # docker sandbox is gone everywhere danno runs. (env DANNO_SANDBOX_CLI overrides.)
    cli: Literal["auto", "sbx", "docker"] = "auto"
    # SBX-WORKAROUND(OpenShell#263): resolve a LOCAL Ollama alias (localhost /
    # 127.0.0.1 / ::1 / 0.0.0.0 / host.docker.internal / gateway.docker.internal) to
    # the host's routable LAN IP for sbx, which has no host.docker.internal→localhost
    # rewrite and can't route the link-local alias. A concrete IP/hostname is always
    # used literally. REMOVE this option + the resolver once sbx routes
    # host.docker.internal.
    resolve_ollama_host: bool = True

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
    agents: dict[str, str | AgentSpec] = Field(default_factory=dict)
    tools: list[Tool] = Field(default_factory=list)
    npm: list[NpmPlugin] = Field(default_factory=list)
    sandbox: Sandbox = Field(default_factory=Sandbox)
    # Agent-general environment table: any KEY=value here is injected into the
    # env-file of every config-driven agent (opencode/claurst/occ — NOT claude,
    # whose auth is injected separately). Values MAY embed {env:VAR} host
    # indirection, resolved at assembly time (see sandbox.assemble_harness_env).
    # Keys are OS env var names, so they are exempt from the no-'/' danno-name
    # rule and from _check_references below.
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_references(self) -> DannoConfig:
        # danno names never contain '/': that is the bit that disambiguates a bare
        # [models] reference from a raw OpenCode ref (e.g. anthropic/claude-sonnet-4-6)
        # in an [agents] value. Guard it at the boundary so the rule can't silently
        # break (Working Rule 8).
        for kind, names in (
            ("backend", self.backends),
            ("model", self.models),
            ("agent", self.agents),
        ):
            for name in names:
                if "/" in name:
                    raise ValueError(f"{kind} name '{name}' must not contain '/'")
        for model_name, model in self.models.items():
            if model.backend not in self.backends:
                raise ValueError(
                    f"model '{model_name}' references unknown backend '{model.backend}'"
                )
        for agent, value in self.agents.items():
            # The model ref is the string value itself, or the rich form's `model`
            # field (which may be unset — e.g. an agent that only pins mode/permission
            # and lets a markdown def or built-in supply the model).
            ref = value if isinstance(value, str) else value.model
            # A '/' marks a raw OpenCode ref (passed through verbatim); otherwise the
            # value names a [models] entry, which must exist.
            if ref is not None and "/" not in ref and ref not in self.models:
                raise ValueError(f"agent '{agent}' references unknown model '{ref}'")
        return self
