"""Typed data model for the per-provider handshake protocol.

A handshake asks a provider (an LM backend or an MCP server) three questions —
can I reach + authenticate to you, what models do you serve, and what does
each model actually support — and returns a :class:`HandshakeReport`.

Per-model CAPABILITY facts no longer live as flat fields here — that was
``ModelProfile``, deleted per the model-capabilities brief (Part 4: "The
per-provider ``supports_vision``, ``max_tokens_default`` and similar flags are
deleted. ``handshake/model.py:ModelProfile`` is split into these records.").
An adapter now evidences a discovered model as a
:class:`~clio_agent.providers.capabilities.records.ModelCapabilities`
(``source="server_report"``) plus a
:class:`~clio_agent.providers.capabilities.records.DeploymentCapabilities`
pair (:class:`DiscoveredModelFacts`), written into the shared record store
(:mod:`clio_agent.providers.capabilities.invalidation`) as a side effect of the
handshake. :class:`HandshakeReport` itself keeps only bare identity
(:class:`DiscoveredModel`: the wire id, its own aliases, whether it's loaded,
and the raw discovery row for the handful of purely-identity fields — a
deployment name, a revision string — that were never capability facts) plus
provenance (`models_source`, `generated_at`); every capability question routes
through :func:`clio_agent.providers.capabilities.accessor.get_effective_capabilities`
instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from clio_agent.providers.capabilities.records import (
        DeploymentCapabilities,
        ModelCapabilities,
    )


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


@dataclass(frozen=True)
class DiscoveredModel:
    """One model a handshake found on a provider: bare identity, not capability.

    Attributes:
        id: The id the SERVER uses on the wire (a
            :class:`~clio_agent.providers.identity.DeploymentKey`'s ``model_id``).
        is_loaded: Whether this model is the (or a) currently loaded one, when
            the provider reports load state.
        aliases: This model's own recorded aliases (e.g. claude_code CLI values
            like ``"sonnet"``), sourced from the SAME discovery evidence that
            produced ``id`` — never a hand-typed table. Empty for providers
            with no alias concept.
        evidence_generated_at: When THIS model's row was actually evidenced —
            a persisted overlay/last-good row's own timestamp, or empty when
            the evidence is this handshake run itself (the report's own
            ``generated_at`` applies then).
        raw: The provider's raw discovery row, kept for the handful of
            purely-identity fields (a deployment/owned_by name, a revision
            string, ``capability_evidence`` provenance, CLI catalog metadata
            like ``supported_reasoning_efforts``) that are not capability
            facts and so have no home in the capability records.
    """

    id: str
    is_loaded: bool = False
    aliases: tuple[str, ...] = ()
    evidence_generated_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DiscoveredModelFacts:
    """One ``discover_model_config`` result: identity plus what it evidenced.

    ``model`` carries ``source="server_report"`` facts about the WEIGHTS
    (shared across every endpoint that serves them); ``deployment`` carries
    facts about how THIS server is running them right now. Both are written
    into the shared record store by
    :class:`~clio_agent.providers.handshake.base.ProviderHandshake` so the
    capabilities accessor can see them; :class:`HandshakeReport` itself only
    keeps ``discovered``.
    """

    discovered: DiscoveredModel
    model: "ModelCapabilities"
    deployment: "DeploymentCapabilities"


def raw_aliases(raw: dict[str, Any]) -> tuple[str, ...]:
    """Return a discovery row's own recorded CLI aliases (``cli_values``), defensively typed.

    ``raw`` is passthrough evidence from a handshake/overlay payload, not a
    validated model -- a malformed or absent ``cli_values`` (not a list, or a
    list with non-string entries) must degrade to "no aliases" rather than
    letting a stray string be iterated character-by-character into false alias
    matches. Adapters call this when building a
    :class:`DiscoveredModel`'s ``aliases`` from their raw discovery row.
    """

    values = raw.get("cli_values")
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values if isinstance(value, str) and value)


def resolve_model_id(candidates: Iterable[tuple[str, Sequence[str]]], query: str) -> str:
    """Resolve a configured model id or alias to its catalog/handshake canonical id.

    ``candidates`` is an iterable of ``(canonical_id, aliases)`` pairs drawn from
    ONE evidence source for ONE provider -- a :class:`HandshakeReport`'s own
    :class:`DiscoveredModel` rows (``.id``, ``.aliases``) or a provider
    catalog's own model rows (``row["model_id"]``, ``row["aliases"]``). Both
    alias lists are populated from the same discovery evidence (#1436's
    ``cli_values``/``aliases``), so this reads existing alias data rather than
    maintaining a second, hand-typed alias table.

    A match is always an EXACT string match against the canonical id or one of
    its own recorded aliases -- never a keyword, prefix, or substring match, so
    an unrelated id (``"sonnet-x"``) can never be mistaken for a real alias
    (``"sonnet"``). ``query`` is returned unchanged when nothing matches: an
    unknown model id stays unknown rather than being coerced onto some other
    candidate.

    This is the ONE place a configured model value (which may be an alias) is
    reconciled against discovered model identity — every capability decision
    that needs to match a configured id against catalog/handshake evidence
    routes through here (directly, or through
    :meth:`HandshakeReport.model` / the model-link rules in
    :mod:`clio_agent.providers.capabilities.link`) so they cannot disagree
    about which model a configured alias names.
    """

    if not query:
        return query
    for candidate_id, aliases in candidates:
        if candidate_id == query or query in aliases:
            return candidate_id
    return query


@dataclass(frozen=True)
class HandshakeReport:
    """The result of a provider handshake. Never raised — failures are encoded in state."""

    provider_id: str
    provider_kind: str
    connectivity: ConnectivityState
    auth: AuthState
    #: The endpoint this report is about (:class:`~clio_agent.providers.identity.EndpointKey`'s
    #: second half). Needed so a capability lookup for one of ``models`` can be
    #: keyed correctly — the report is the one place that already carries it.
    api_base: str = ""
    latency_ms: float | None = None
    error: str | None = None
    models: tuple[DiscoveredModel, ...] = ()
    #: Catalog-level provenance, reported by the handshake that ran rather than
    #: assumed: ``live`` (this run probed the provider), ``overlay`` (persisted
    #: evidence from an earlier explicit discovery run), ``static`` (the
    #: compiled-in registry catalog -- candidates, not evidence), or
    #: ``unavailable``.
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

    def model(self, model_id: str) -> DiscoveredModel | None:
        """Return the discovered row for ``model_id``, or None.

        ``model_id`` may be an exact row id or one of a row's own recorded
        aliases (e.g. claude_code CLI values like ``"sonnet"`` for
        ``"claude-sonnet-5"``) -- see :func:`resolve_model_id`, the shared
        resolution point every alias-tolerant model lookup routes through.
        """
        canonical = resolve_model_id(
            ((row.id, row.aliases) for row in self.models),
            model_id,
        )
        for row in self.models:
            if row.id == canonical:
                return row
        return None

    def match_model(self, model_id: str) -> DiscoveredModel | None:
        """Resolve ``model_id`` to a discovered row with the fuller bind-time tolerance.

        Tries, in order: an exact id/alias match (:meth:`model`); a vendor-prefix
        basename match (``"gpt-oss-120b"`` matches a discovered
        ``"openai/gpt-oss-120b"``); the sole discovered model, when exactly one
        was found. Both :meth:`clio_agent.config.LMProviderConfig.apply_handshake`
        and :mod:`clio_agent.providers.resolver` route through this single
        method so they cannot disagree about which model a handshake resolved.
        """
        if not self.models:
            return None
        found = self.model(model_id)
        if found is not None:
            return found
        want = model_id.rsplit("/", 1)[-1].lower()
        found = next(
            (row for row in self.models if row.id.rsplit("/", 1)[-1].lower() == want), None
        )
        if found is not None:
            return found
        return self.models[0] if len(self.models) == 1 else None

    def models_wire(self) -> dict[str, Any]:
        """Render to the legacy model-picker contract (``{"models", "source", "error"}``).

        Preserves the wire shape ``to_models_wire`` used to produce (so
        existing ``GET /v1/providers/{id}/models`` clients keep working)
        but sources every per-model capability field from
        :func:`clio_agent.providers.capabilities.accessor.get_effective_capabilities`
        instead of a flat ``ModelProfile`` — there is no longer a flat field
        for this method to read.
        """
        from clio_agent.providers.capabilities.accessor import (  # noqa: PLC0415
            get_effective_capabilities,
        )

        if not self.ok and not self.models:
            source = "unavailable"
        else:
            source = self.models_source
        rows: list[dict[str, Any]] = []
        for m in self.models:
            row: dict[str, Any] = {"id": m.id, "name": m.id}
            effective = get_effective_capabilities(self.provider_id, self.api_base, m.id)
            if effective.context.known:
                row["context_window"] = effective.context.value
            if effective.tools.value:
                row["native_tool_calling"] = True
            if effective.thinking.known:
                row["is_reasoning"] = True
            quantization = m.raw.get("quantization")
            if quantization:
                row["quantization"] = quantization
            if m.is_loaded:
                row["loaded"] = True
            row["context_source"] = effective.context.decided_by
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
            from clio_agent.providers.capabilities.accessor import (  # noqa: PLC0415
                get_effective_capabilities,
            )

            model_details = []
            for m in self.models[:20]:
                effective = get_effective_capabilities(self.provider_id, self.api_base, m.id)
                model_details.append(
                    {
                        "id": m.id,
                        "context_window": effective.context.value,
                        "is_reasoning": effective.thinking.known,
                        "native_tool_calling": bool(effective.tools.value),
                        "context_source": effective.context.decided_by,
                    }
                )
            details["models"] = model_details
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


__all__ = [
    "AuthState",
    "ConnectivityState",
    "DiscoveredModel",
    "DiscoveredModelFacts",
    "HandshakeReport",
    "raw_aliases",
    "resolve_model_id",
]
