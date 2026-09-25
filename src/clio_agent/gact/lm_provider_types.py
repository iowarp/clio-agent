"""Pydantic wire types for LM provider configuration routes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class LMProviderConfigurationField(BaseModel):
    """One non-secret option rendered in a provider settings form."""

    id: str
    label: str
    description: str = ""
    placeholder: str = ""
    required: bool = False


class LMProviderPreset(BaseModel):
    """One row in the TUI's provider picker. ``requires_api_key``
    tells the modal whether to render the api_key field; some
    presets (LM Studio, Ollama, local vLLM) don't need one."""

    id: str
    provider_id: str = ""
    label: str
    provider: str
    litellm_prefix: str = ""
    api_base: str
    suggested_model: str
    requires_api_key: bool = True
    api_key_env: str = ""
    auth_method: Literal["none", "api_key", "oauth", "subscription"] = "api_key"
    #: Sign-in service name for the sign-in detail ("Globus Auth"); never part of ``label``.
    auth_label: str = ""
    is_authenticated: bool = False
    description: str = ""
    status: Literal[
        "ready",
        "missing_key",
        "auth_required",
        "auth_check_required",
        "unavailable",
        "unknown",
    ] = "unknown"
    status_message: str = ""
    supports_live_catalog: bool = True
    supports_vision: bool = False
    configuration_fields: list[LMProviderConfigurationField] = Field(default_factory=list)
    supports_runtime_sizing: bool = False
    managed_service_id: str = ""


class LMProviderInfo(BaseModel):
    """GET /v1/providers/lm body: current LM config state + the preset list the
    TUI's picker shows. ``api_key`` is never echoed back.
    ``thinking_level`` (off|low|medium|high, null=unset) is the provider-generic
    reasoning control; ``thinking_budget`` is the explicit token override; and
    ``thinking_effective`` is the resolved per-provider effect (#895), so the
    knob is never invisible on the wire — including a typed ``unsupported`` note.
    """

    configured: bool
    provider_id: str = ""
    provider: str = ""
    api_base: str = ""
    model: str = ""
    #: The catalog model id ``model`` actually resolves to (claude_code CLI
    #: aliases like "sonnet" -> "claude-sonnet-5"); equal to ``model`` when the
    #: provider has no alias concept or none is known yet.
    resolved_model_id: str = ""
    temperature: float = 0.0
    max_tokens: int = 0
    context_length: int = 0
    chosen_context: int | None = None
    context_window: int | None = None
    is_reasoning: bool = False
    native_tool_calling: bool = False
    thinking_level: str | None = None
    #: ``"user"`` when a person set ``thinking_level``; ``None`` for a shipped or
    #: provider default -- clients show those as defaults, not as a choice.
    thinking_level_source: str | None = None
    thinking_effective: str = ""
    thinking_budget: int = 0
    transport: Literal["sdk", "websocket", "sse"] | None = None
    state: Literal["idle", "configuring", "ready", "error"] = "idle"
    status_message: str = ""
    error: str = ""
    operation_id: str = ""
    provider_options: dict[str, str] = Field(default_factory=dict)
    presets: list[LMProviderPreset] = Field(default_factory=list)

    @model_validator(mode="after")
    def _resolve_model_alias(self) -> "LMProviderInfo":
        """Auto-derive ``resolved_model_id`` when a caller does not set it.

        Keeps the alias resolution (claude_code CLI values like "sonnet" ->
        "claude-sonnet-5") in the ONE place that owns it
        (``resolve_configured_model_id``) instead of every route that builds
        an ``LMProviderInfo`` having to remember to call it.
        """
        if not self.resolved_model_id and self.model:
            from clio_agent.providers.reasoning_levels import (  # noqa: PLC0415
                resolve_configured_model_id,
            )

            self.resolved_model_id = resolve_configured_model_id(self.provider, self.model)
        return self


class LMProviderRequest(BaseModel):
    """PUT /v1/providers/lm body. Provider is one of
    `openai|anthropic|openrouter|lm_studio|ollama|...` — anything
    LiteLLM understands. ``api_key`` is required for cloud
    providers; locally-OpenAI-compatible backends (LM Studio,
    Ollama, local vLLM) tolerate any non-empty string.

    ``temperature`` + ``max_tokens`` are forwarded to dspy.LM so
    the user can tune behaviour from the TUI without touching env
    vars. Defaults match LMProviderConfig's defaults
    (temperature=0.0 — deterministic, structured/tool-calling agentic
    output; max_tokens=0 omits the client output cap).
    """

    provider: str
    provider_id: str = ""
    api_base: str
    model: str
    api_key: str = "x"
    provider_options: dict[str, str] = Field(default_factory=dict)
    temperature: float = 0.0
    max_tokens: int = 0
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    context_length: int = 0
    parallel: int = 0
    turn_timeout_s: float = 0.0
    transport: str | None = None
    # WHICH of a multi-transport provider's implementations to bind (S1b).
    # Only the ``codex`` provider reads this today (``"sdk"`` | ``"direct"``,
    # default ``"direct"``) -- distinct from ``transport`` above, which is a
    # provider's own internal delivery choice (codex direct's websocket/sse,
    # claude_code's sdk). Named ``variant`` to mirror ``ModelRef.variant``,
    # which a session/message model ref uses to request the same transport.
    variant: str = ""
    thinking_level: (
        Literal["off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] | None
    ) = None
    thinking_budget: int = 0
