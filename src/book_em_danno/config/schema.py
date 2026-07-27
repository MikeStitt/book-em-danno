"""Pydantic models mirroring danno.toml — the declarative source of truth.

Validation lives at this boundary (Working Rule 7/8): unknown keys and dangling
references fail loud rather than producing a subtly wrong opencode.jsonc.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Overrides(BaseModel):
    """Per-harness escape hatch, co-located with the element it modifies.

    Each `[<element>.overrides.<harness>]` payload is DEEP-MERGED (override wins;
    objects merge, scalars/arrays replace) into that element's generated block for
    that harness, at generation time, INSIDE the danno:managed markers — so it stays
    idempotent, reversible (remove it → next generate reverts), and visible in the
    diff (see `generate.deep_merge` / `generate.override_warnings`).

    The payload below each harness key is intentionally OPEN (that is the hatch); the
    harness KEYS are CLOSED to the harnesses that HAVE a danno-generated config surface
    (opencode's opencode.jsonc, claurst's registry overlay) — a typo or an out-of-scope
    harness (e.g. `claude`, which has no generated config file) fails loud. The valid key
    set is the harness registry's override-capable set (`Harness.overrides_key`), so
    adding a config-generating harness needs no edit here. Attached to the elements
    whose danno.toml section maps 1:1 to a generated region: `[backends.<n>]`,
    `[models.<n>]`, `[agents.<n>]`, `[defaults]`."""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="before")
    @classmethod
    def _reject_out_of_scope_harness(cls, data: Any) -> Any:
        """Fail loud on an override key that isn't a config-generating harness (Working
        Rule 8). The valid set comes from the harness registry, imported lazily: the
        registry lives in danno_validator, which imports book_em_danno, so a module-load
        import here would cycle (same reason `commands/sandbox.py` imports it locally)."""
        if isinstance(data, dict):
            from danno_validator.harnesses import all_names, get

            valid = {key for n in all_names() if (key := get(n).overrides_key)}
            unknown = sorted(k for k in data if k not in valid)
            if unknown:
                allowed = ", ".join(sorted(valid))
                raise ValueError(
                    f"override harness key(s) {unknown} out of scope; a config-generating "
                    f"harness is required. Valid: {allowed}."
                )
        return data


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target: str = "."


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    default_agent: str = "pm"
    profile: Literal["hybrid", "cloud-only", "local-only"] = "hybrid"
    # Escape hatch for opencode.jsonc's danno-owned TOP-LEVEL keys ($schema,
    # default_agent, model, small_model). claurst has no danno-owned top-level surface.
    overrides: Overrides | None = None


class OllamaBackend(BaseModel):
    """Local models via OpenCode's @ai-sdk/openai-compatible provider. IMPLEMENTED.

    Note what is deliberately absent: there is NO knob here for Ollama's REAL context
    window or for streaming/thinking. Under the OpenAI-compatible `/v1` API a body
    `num_ctx` is ignored — Ollama loads the model at its FULL context — and opencode
    always streams (it hardcodes `stream: true`). The real window / RAM lever is an
    Ollama Modelfile variant, out of scope here. The client-side context/output budget
    (`limit.context`/`limit.output`) and reasoning are per-model (see Model)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["ollama"]
    base_url: str
    # opencode.jsonc `provider.<n>` / claurst registry provider-entry escape hatch.
    overrides: Overrides | None = None


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
    env-file, never in danno.toml or the committed opencode.jsonc). The client-side
    context/output budget is per-model (see Model)."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["openai"]
    base_url: str
    api_key_env: str
    # The OpenAI wire protocol(s) this endpoint actually serves (its `offers` in the
    # speakable-matrix predicate). api.openai.com serves both Chat completions AND the
    # Responses API; NVIDIA NIM / vLLM / most compatible hosts serve Chat only. Default is the
    # universal Chat baseline — declare `wire = ["chat", "responses"]` for an endpoint that
    # also serves Responses (required for a codex cloud row). A harness can dial a model here
    # only over a protocol in BOTH this set and what the harness speaks. See `backend_wire_offers`.
    wire: frozenset[Literal["chat", "responses"]] = frozenset({"chat"})
    # claurst's built-in provider id this backend maps to (e.g. "openai", "groq").
    # claurst is launched `-m <provider>/<tag>` and resolves <provider> against its OWN
    # registry, so danno must name it. When unset danno INFERS it from the host (NVIDIA
    # NIM → "nvidia"); a generic OpenAI-compatible host (api.openai.com, a local proxy)
    # has no inference, so declare it here. Data-driven (#106): adding a claurst provider
    # is a danno.toml edit, not a source change. Emitted into claurst's models.json
    # overlay as a self-describing entry (its `api` = base_url, `env` = [api_key_env]).
    claurst_provider: str | None = None
    # opencode.jsonc `provider.<n>` / claurst registry provider-entry escape hatch.
    overrides: Overrides | None = None


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
    Dialing an inert model with a config-driven harness (opencode/claurst) fails
    loud, since there is no endpoint to reach.
    """

    model_config = ConfigDict(extra="forbid")
    kind: Literal["inert"]


Backend = Annotated[
    OllamaBackend | LlamacppBackend | OpenAIBackend | InertBackend,
    Field(discriminator="kind"),
]


def backend_wire_offers(backend: Backend) -> frozenset[str]:
    """The wire protocols a backend serves (its `offers` in the speakable-matrix predicate).

    `kind` decides it for all but OpenAI-compatible hosts, where only the author knows whether
    the endpoint serves the Responses API (api.openai.com: yes; NVIDIA NIM / vLLM: Chat only) —
    hence `OpenAIBackend.wire`. Ollama's `/v1` serves both Chat and Responses on a modern build;
    the codex path fail-loud probes `/v1/responses` at run time (`ollama.responses_api_ready`),
    so the static offer stays broad. `inert` is the claude reference row → Anthropic-native.
    """
    if isinstance(backend, OpenAIBackend):
        return frozenset(backend.wire)
    if isinstance(backend, OllamaBackend):
        return frozenset({"chat", "responses"})
    if isinstance(backend, LlamacppBackend):
        return frozenset({"chat"})
    if isinstance(backend, InertBackend):
        return frozenset({"anthropic"})
    raise ValueError(f"unknown backend kind for wire offers: {backend!r}")  # pragma: no cover


class Model(BaseModel):
    """A named (backend, tag) pair. `tag` is the model id on the backend.

    `context_budget`/`output_limit` are the client-side budget the harness uses to
    trim/compact the conversation, emitted as `limit.context`/`limit.output` (opencode
    and claurst alike). They are REQUIRED on a model whose backend actually dials an
    endpoint (`ollama`/`openai`) and FORBIDDEN on one that does not (`inert`/`llamacpp`,
    which emit no limit block) — enforced in `DannoConfig._check_references`, fail loud,
    no silent default. (`limit.context` is opencode's CLIENT-SIDE belief of the window;
    under `/v1` it does NOT change what Ollama loads — the real window is a Modelfile
    lever. usable input ≈ context_budget − output_limit.)

    `reasoning_effort` (ollama only) is emitted as the model-level camelCase
    `options.reasoningEffort`, which @ai-sdk/openai-compatible spreads raw into the
    `/v1` request body where Ollama honors it. "none" disables the thinking trace
    (faster, and avoids the opencode #21903 reasoning-field hang); leave unset to
    forward nothing. Note: gpt-oss-style models reject "none" — use low/medium/high
    for those (documented here, not validated, since the model id isn't known)."""

    model_config = ConfigDict(extra="forbid")
    backend: str
    tag: str | None = None
    context_budget: int | None = None
    output_limit: int | None = None
    reasoning_effort: Literal["none", "low", "medium", "high"] | None = None
    # The wire protocol(s) this model REQUIRES, when narrower than its backend offers (e.g. a
    # reasoning model reachable only via the Responses API on a dual-protocol endpoint). Feeds
    # the predicate `requires ⊆ (harness.speaks ∩ backend.offers)`. Default None = inherit (no
    # extra constraint beyond what the harness and backend already share).
    requires_wire: frozenset[Literal["chat", "responses"]] | None = None
    # opencode.jsonc `provider.<backend>.models.<tag>` / claurst model-entry escape
    # hatch (e.g. options.max_completion_tokens for an OpenAI o-series model).
    overrides: Overrides | None = None


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
    # opencode.jsonc `agent.<name>` / claurst settings.json agent-entry escape hatch.
    # Excluded from the verbatim field dump the generator emits (it is not an opencode
    # agent field); see generate._danno_agent_fields / _danno_doc.
    overrides: Overrides | None = None


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
    # env-file of every config-driven agent (opencode/claurst — NOT claude,
    # whose auth is injected separately). Values MAY embed {env:VAR} host
    # indirection, resolved at assembly time (see sandbox.assemble_harness_env).
    # Keys are OS env var names, so they are exempt from the no-'/' danno-name
    # rule and from _check_references below.
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_references(self) -> DannoConfig:
        # claurst has no danno-owned TOP-LEVEL config surface (danno owns only the
        # `agents` key of settings.json + the models.json overlay), so a top-level
        # claurst override would be silently ignored — reject it (Working Rule 8).
        if (
            self.defaults.overrides is not None
            and getattr(self.defaults.overrides, "claurst", None) is not None
        ):
            raise ValueError(
                "[defaults.overrides.claurst] has no target: claurst has no danno-owned "
                "top-level config. Use per-backend/model/agent overrides instead."
            )
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
            backend = self.backends[model.backend]
            # Budgets and overrides are only meaningful for a backend that danno dials
            # and emits a config block for (ollama/openai). Required there, forbidden
            # elsewhere (inert/llamacpp) — fail loud either way, never a silent default
            # nor a silently-ignored override.
            if isinstance(backend, OllamaBackend | OpenAIBackend):
                missing = [
                    field_name
                    for field_name, value in (
                        ("context_budget", model.context_budget),
                        ("output_limit", model.output_limit),
                    )
                    if value is None
                ]
                if missing:
                    raise ValueError(
                        f"model '{model_name}' on {backend.kind} backend needs "
                        f"{' and '.join(missing)} (set them under [models.{model_name}])"
                    )
            else:
                if model.context_budget is not None or model.output_limit is not None:
                    raise ValueError(
                        f"model '{model_name}' sets context_budget/output_limit, which is "
                        f"meaningless on a {backend.kind} backend (it emits no limit block)"
                    )
                if model.overrides is not None:
                    raise ValueError(
                        f"model '{model_name}' sets overrides, which is meaningless on a "
                        f"{backend.kind} backend (it emits no config block to merge into)"
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
