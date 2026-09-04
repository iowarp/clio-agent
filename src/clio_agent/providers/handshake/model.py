"""Typed data model for the per-provider handshake protocol.

A handshake asks a provider (an LM backend or an MCP server) three questions —
can I reach + authenticate to you, what models/tools do you serve, and what is
each model's real configuration — and returns a :class:`HandshakeReport`. The
report carries per-model :class:`ModelProfile` records whose ``context_window``,
``loaded_context_window`` and capability flags drive clio's runtime config
(``max_tokens`` sizing, LM Studio load-sizing, reasoning/tool decisions).

The shapes here deliberately mirror ``clio_agent.runtime.status.IntegrationStatus``
so a report can be rendered into the existing ``/v1/health`` surface, and
``to_models_wire`` preserves the legacy ``{"models", "source", "error"}`` contract
the TUI model picker already consumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConnectivityState(str, Enum):
    """Whether the provider endpoint could be reached."""

    OK = "ok"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"  # deliberately not probed (e.g. cloud/argonne passive mode)


class AuthState(str, Enum):
    """Whether authentication material was present and accepted."""

    OK = "ok"
    MISSING = "missing"  # no credential available
    REJECTED = "rejected"  # credential present but the provider refused it
    NOT_REQUIRED = "not_required"  # local/no-auth provider
    DEFERRED = "deferred"  # token stored but not validated (argonne passive)


# Provenance of a model's ``context_window`` value. The factory tries these in
# strict order and stops at the first that yields a value.
ContextSource = str  # "live" | "models.dev" | "marketplace" | "static"


@dataclass(frozen=True)
class ModelProfile:
    """Discovered configuration for a single model on a provider.

    ``context_window`` is the backend's hard ceiling (vLLM ``max_model_len`` /
    LM Studio ``max_context_length``). ``loaded_context_window`` is the *runtime*
    window an already-loaded model is serving (LM Studio ``loaded_context_length``)
    — this is the value that actually bounds a request, and the source of the
    "loaded at 8192 while max is 262144" class of failures. ``chosen_context`` is
    the GPU-aware value clio decided to use (``min(window, cap, override)``); it is
    the authoritative, queryable "active context limit".
    """

    id: str
    context_window: int | None = None
    loaded_context_window: int | None = None
    chosen_context: int | None = None
    output_limit: int | None = None
    is_reasoning: bool = False
    reasoning_param: str | None = None
    native_tool_calling: bool = False
    tool_call_parser: str | None = None
    quantization: str | None = None
    arch: str | None = None
    capabilities: tuple[str, ...] = ()
    is_loaded: bool = False
    context_source: ContextSource = "live"
    #: When this profile's CAPABILITY evidence was actually produced. A handshake
    #: that reads persisted discovery evidence (the CLI-provider overlay) carries
    #: the timestamp of the probe that produced it, not the wall clock of the
    #: read; empty when the evidence is the handshake run itself.
    evidence_generated_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_context_window(self) -> int | None:
        """The window that actually bounds a request: loaded if known, else the ceiling."""
        return self.loaded_context_window or self.context_window


@dataclass(frozen=True)
class HandshakeReport:
    """The result of a provider handshake. Never raised — failures are encoded in state."""

    provider_id: str
    provider_kind: str
    connectivity: ConnectivityState
    auth: AuthState
    latency_ms: float | None = None
    error: str | None = None
    models: tuple[ModelProfile, ...] = ()
    #: Catalog-level provenance, reported by the handshake that ran rather than
    #: assumed: ``live`` (this run probed the provider), ``overlay`` (persisted
    #: evidence from an earlier explicit discovery run), ``static`` (the
    #: compiled-in registry catalog -- candidates, not evidence), or
    #: ``unavailable``. It was previously hardcoded to ``live`` whenever ANY
    #: model existed, which made the zero-network NoOp/CliCatalog handshakes
    #: claim a live probe they never performed.
    models_source: str = "live"
    #: Wall clock of THIS handshake run.
    generated_at: str = ""
    #: When the model evidence was actually produced. Equals ``generated_at``
    #: for a real live probe; for overlay-sourced evidence it is the timestamp
    #: the discovery run persisted, so a cached catalog cannot present itself as
    #: freshly generated.
    evidence_generated_at: str = ""

    @property
    def ok(self) -> bool:
        """Reachable and authenticated (or auth not required / deferred)."""
        return self.connectivity == ConnectivityState.OK and self.auth in {
            AuthState.OK,
            AuthState.NOT_REQUIRED,
            AuthState.DEFERRED,
        }

    def model(self, model_id: str) -> ModelProfile | None:
        """Return the profile for ``model_id`` (exact match), or None."""
        for profile in self.models:
            if profile.id == model_id:
                return profile
        return None

    def to_models_wire(self) -> dict[str, Any]:
        """Render to the legacy model-picker contract, with per-model fields added.

        Preserves ``{"models": [...], "source": ..., "error": ...}`` so existing
        ``GET /v1/providers/{id}/models`` consumers keep working; the discovered
        config (``context_window``, ``chosen_context``, ``is_reasoning``, ...) rides
        along as additive keys on each model dict.
        """
        if not self.ok and not self.models:
            source = "unavailable"
        else:
            source = self.models_source
        rows: list[dict[str, Any]] = []
        for m in self.models:
            row: dict[str, Any] = {"id": m.id, "name": m.id}
            if m.context_window is not None:
                row["context_window"] = m.context_window
            if m.loaded_context_window is not None:
                row["loaded_context_window"] = m.loaded_context_window
            if m.chosen_context is not None:
                row["chosen_context"] = m.chosen_context
            if m.is_reasoning:
                row["is_reasoning"] = True
            if m.native_tool_calling:
                row["native_tool_calling"] = True
            if m.quantization:
                row["quantization"] = m.quantization
            if m.is_loaded:
                row["loaded"] = True
            row["context_source"] = m.context_source
            rows.append(row)
        return {"models": rows, "source": source, "error": self.error}

    def to_integration_status(self) -> Any:
        """Render to a ``runtime.status.IntegrationStatus`` row for ``/v1/health``.

        Imported lazily to keep this module free of the heavier status module.
        """
        from clio_agent.runtime.status import (  # noqa: PLC0415
            IntegrationState,
            IntegrationStatus,
        )

        if self.connectivity == ConnectivityState.SKIPPED:
            state = IntegrationState.SKIPPED
        elif self.auth == AuthState.REJECTED:
            state = IntegrationState.MISCONFIGURED
        elif self.connectivity in {ConnectivityState.UNREACHABLE, ConnectivityState.TIMEOUT}:
            state = IntegrationState.UNAVAILABLE
        elif self.auth == AuthState.MISSING:
            state = IntegrationState.MISCONFIGURED
        else:
            state = IntegrationState.READY

        if self.error:
            summary = self.error
        elif self.models:
            summary = f"{len(self.models)} model(s) discovered"
        else:
            summary = f"{self.provider_kind} reachable"

        caps = ["chat-completions", "models"]
        details: dict[str, Any] = {
            "provider": self.provider_id,
            "connectivity": self.connectivity.value,
            "auth": self.auth.value,
        }
        if self.latency_ms is not None:
            details["latency_ms"] = round(self.latency_ms, 1)
        if self.models:
            details["models"] = [
                {
                    "id": m.id,
                    "context_window": m.context_window,
                    "chosen_context": m.chosen_context,
                    "is_reasoning": m.is_reasoning,
                    "native_tool_calling": m.native_tool_calling,
                    "context_source": m.context_source,
                }
                for m in self.models[:20]
            ]
        next_action = ""
        if state == IntegrationState.MISCONFIGURED and self.auth == AuthState.REJECTED:
            next_action = "Provider rejected the credential; refresh or re-enter the API key/token."
        elif state == IntegrationState.UNAVAILABLE:
            next_action = "Provider endpoint was unreachable; check the api_base and network."
        return IntegrationStatus(
            name="lm_provider",
            state=state,
            summary=summary,
            config_source="handshake",
            next_action=next_action,
            capabilities=caps,
            details=details,
        )
