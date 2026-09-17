"""A2UI 0.9 capability negotiation: the GACT binding (S3).

docs/design/a2ui-compat-campaign-2026-09.md S3 — the owner module for the
official capability-negotiation objects the protocol defines as TRANSPORT
METADATA (A2A puts them in message metadata; GACT's binding is written down
in ``docs/gact/a2ui-binding.md``):

* ``a2uiClientCapabilities`` (renderer -> agent): what catalogs the client
  can render, preference-ordered, under metadata key ``a2uiClientCapabilities``.
* ``a2uiAgentCapabilities`` / :func:`agent_capabilities` (agent -> renderer):
  what catalogs THIS server can produce, ``{"v0.9": {supportedCatalogIds,
  acceptsInlineCatalogs: false}}``.
* ``a2uiClientDataModel`` (renderer -> the creating server ONLY): the
  client's live surface data model, under metadata key ``a2uiClientDataModel``,
  carried through onto an accepted message as ``a2ui_client_data_model``.

This module never decides routing/completion (⚑ #1) — it VALIDATES (schema),
REMEMBERS (session metadata, no fifth store — RULE 4), and SELECTS (first
client-preferred catalog this session can produce). Every degradation is a
typed reason recorded through the S2 ledger
(:func:`clio_agent.gact.a2ui_catalogs.reasons.record_a2ui_catalog_reason`, via
the registry's ``record_session_reason`` so it also lands in the per-session
ledger ``CatalogRegistry.session_reasons`` reads back) — never silently
dropped, coerced, or defaulted (no-silent-fallback ground rule).

**Session-scoped memory.** ``a2uiClientCapabilities`` persists on
``Session.metadata[A2UI_CLIENT_CAPABILITIES_METADATA_KEY]`` through the
existing :meth:`~clio_agent.gact.sessions.SessionStore.update` path (shallow
metadata merge + flush-to-disk), so "last advertisement wins" and survives a
process restart exactly like ``goal``/``loop`` state (RULE 4: no fifth
store). It is never stored in a NEW structure.

**Sub-agent stripping.** :func:`strip_renderer_metadata` removes the raw
wire keys (``a2uiClientCapabilities``, ``a2uiClientDataModel``) AND the
accepted-message's renamed data-model key (``a2ui_client_data_model``) from
any metadata mapping about to ride onto a spawned child/expert turn — the
protocol's own rule ("sent exclusively to the server that created the
surface... orchestrators MUST strip it before sub-agents"), applied at every
site :mod:`clio_agent.gact.turn_spawn` and
:mod:`clio_agent.gact.agent_message_transport` hand a metadata mapping to a
child session.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from clio_schemas.a2ui.v0_9_1.capabilities import A2UIAgentCapabilities, A2UIClientCapabilities
from clio_schemas.a2ui.v0_9_1.data_model import A2UIClientDataModel
from pydantic import ValidationError as _PydanticValidationError

from clio_agent.gact.a2ui_catalogs.activation import session_producible_catalog_ids

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.a2ui_catalogs.registry import CatalogRegistry

#: ``Session.metadata`` key holding the last-remembered client capabilities
#: object (no fifth store — RULE 4, mirrors ``goal.GOAL_METADATA_KEY``).
A2UI_CLIENT_CAPABILITIES_METADATA_KEY = "a2ui_client_capabilities"

#: Message-metadata key an accepted message/action carries the validated
#: client data model under (the renamed, normalized form of the wire's
#: ``a2uiClientDataModel``).
A2UI_CLIENT_DATA_MODEL_METADATA_KEY = "a2ui_client_data_model"

#: The raw A2A/renderer transport-metadata keys a client may attach to a
#: message or action. NOT reserved (RESERVED_CLIENT_METADATA_KEYS in
#: ``gact/messaging.py`` stays untouched) — a client is free to send them;
#: this module validates and normalizes them at the door.
A2UI_CLIENT_CAPABILITIES_WIRE_KEY = "a2uiClientCapabilities"
A2UI_CLIENT_DATA_MODEL_WIRE_KEY = "a2uiClientDataModel"

#: Every metadata key :func:`strip_renderer_metadata` removes before a
#: spawned child/expert turn ever sees a mapping derived from a parent
#: message/session: both raw wire keys (defence in depth — a caller that
#: forwards an un-normalized client mapping is still covered) plus the
#: accepted-message's renamed data-model key (the one actually stored once
#: :func:`apply_client_metadata_guards` has run).
RENDERER_METADATA_KEYS: frozenset[str] = frozenset(
    {
        A2UI_CLIENT_CAPABILITIES_WIRE_KEY,
        A2UI_CLIENT_DATA_MODEL_WIRE_KEY,
        A2UI_CLIENT_DATA_MODEL_METADATA_KEY,
    }
)


class A2UICapabilitiesError(ValueError):
    """A typed capabilities/data-model refusal (a door raises this as a 422).

    ``reason`` is a key in
    :data:`clio_agent.gact.a2ui_catalogs.reasons._A2UI_CATALOG_REASON_DEFINITIONS`
    -- the caller is expected to record it via the registry's
    ``record_session_reason`` (never silently swallowed) and translate it
    into an ``ErrorEnvelope`` with that same code.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def parse_client_capabilities(
    metadata: Mapping[str, Any] | None,
) -> A2UIClientCapabilities | None:
    """Validate ``metadata[A2UI_CLIENT_CAPABILITIES_WIRE_KEY]``, or ``None`` if absent.

    Args:
        metadata: A client-supplied metadata mapping (message or action body).

    Returns:
        The validated capabilities object, or ``None`` when the key is absent
        (an absent advertisement is NOT an error here — :func:`select_catalog`
        is where "no advertisement yet" becomes the typed
        ``a2ui_client_capabilities_unknown`` reason, at SELECTION time, per
        the issue's contract).

    Raises:
        A2UICapabilitiesError: ``reason="a2ui_client_capabilities_invalid"`` when
            the value does not validate as the official ``A2UIClientCapabilities``
            shape; ``reason="a2ui_inline_catalogs_unsupported"`` when the client
            sent ``inlineCatalogs`` (this agent advertises
            ``acceptsInlineCatalogs: false`` — the protocol says a client should
            only send them when the agent accepts them).
    """

    if not metadata or A2UI_CLIENT_CAPABILITIES_WIRE_KEY not in metadata:
        return None
    raw = metadata[A2UI_CLIENT_CAPABILITIES_WIRE_KEY]
    try:
        caps = A2UIClientCapabilities.model_validate(raw)
    except _PydanticValidationError as exc:
        raise A2UICapabilitiesError(
            f"{A2UI_CLIENT_CAPABILITIES_WIRE_KEY} is not a valid A2UI 0.9 client "
            f"capabilities object: {exc}",
            reason="a2ui_client_capabilities_invalid",
        ) from exc
    if caps.v0_9.inlineCatalogs:
        raise A2UICapabilitiesError(
            "inlineCatalogs is unsupported: this agent advertises acceptsInlineCatalogs: false",
            reason="a2ui_inline_catalogs_unsupported",
        )
    return caps


def parse_client_data_model(
    metadata: Mapping[str, Any] | None,
) -> A2UIClientDataModel | None:
    """Validate ``metadata[A2UI_CLIENT_DATA_MODEL_WIRE_KEY]``, or ``None`` if absent.

    Shape validation only (S3 scope) — S5 owns ingestion/fold semantics.

    Raises:
        A2UICapabilitiesError: ``reason="a2ui_client_data_model_invalid"`` when
            the value does not validate as the official ``A2UIClientDataModel``
            shape (``{"version": "v0.9"|"v0.9.1", "surfaces": {...}}``).
    """

    if not metadata or A2UI_CLIENT_DATA_MODEL_WIRE_KEY not in metadata:
        return None
    raw = metadata[A2UI_CLIENT_DATA_MODEL_WIRE_KEY]
    try:
        return A2UIClientDataModel.model_validate(raw)
    except _PydanticValidationError as exc:
        raise A2UICapabilitiesError(
            f"{A2UI_CLIENT_DATA_MODEL_WIRE_KEY} is not a valid A2UI 0.9 client data "
            f"model object: {exc}",
            reason="a2ui_client_data_model_invalid",
        ) from exc


def session_requested_send_data_model(app: "FastAPI", session_id: str) -> bool:
    """Return whether any LIVE surface in this session was created with ``sendDataModel``.

    Reads the existing transcript-projected surfaces
    (``app.state.a2ui_store.list_wire``) rather than adding a field to
    ``A2UISurfaceRecord`` — ``sendDataModel`` already rides verbatim on the
    stored ``createSurface`` message (``gact/a2ui.py``'s
    ``allowed_payload_keys``), this only reads it back (no accretion onto the
    S2 owner module).
    """

    store = getattr(app.state, "a2ui_store", None)
    if store is None:
        return False
    for row in store.list_wire(session_id):
        for message in row.get("messages", []) or []:
            if not isinstance(message, Mapping):
                continue
            create = message.get("createSurface")
            if isinstance(create, Mapping) and bool(create.get("sendDataModel")):
                return True
    return False


def remember_client_capabilities(
    app: "FastAPI", session_id: str, caps: A2UIClientCapabilities
) -> None:
    """Persist ``caps`` as the session's last-advertised client capabilities.

    "Last advertisement wins": a later POST with a different
    ``supportedCatalogIds`` list simply overwrites this key (shallow
    ``metadata_patch`` merge), and it survives a restart because
    :meth:`SessionStore.update` flushes to disk on every call (RULE 4, no
    fifth store — mirrors ``goal._put_goal``).
    """

    if app.state.sessions.get(session_id) is None:
        return
    app.state.sessions.update(
        session_id,
        metadata_patch={
            A2UI_CLIENT_CAPABILITIES_METADATA_KEY: caps.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
        },
    )


def apply_client_metadata_guards(
    app: "FastAPI", session_id: str, metadata: Mapping[str, Any] | None
) -> A2UIClientDataModel | None:
    """Validate, remember, and typed-record A2UI renderer transport metadata.

    The ONE door guard every client-writable ingest that MAY carry
    ``a2uiClientCapabilities``/``a2uiClientDataModel`` calls (POST
    ``/messages`` and the A2UI action route): parses + validates
    ``a2uiClientCapabilities`` (remembering it on success regardless of what
    happens afterward in the caller), parses + validates
    ``a2uiClientDataModel`` (refusing it when no live surface in this
    session requested ``sendDataModel``), and records every refusal's typed
    reason through the S2 per-session ledger BEFORE re-raising. A caller
    translates the raised :class:`A2UICapabilitiesError` into its own
    transport's error envelope (HTTP 422).

    Returns:
        The validated client data model, or ``None`` when the request
        carried none. A caller that wants to carry it through onto an
        accepted record (message/action) attaches it itself, under
        :data:`A2UI_CLIENT_DATA_MODEL_METADATA_KEY`.
    """

    registry: "CatalogRegistry | None" = getattr(app.state, "a2ui_catalogs", None)
    try:
        caps = parse_client_capabilities(metadata)
        if caps is not None:
            remember_client_capabilities(app, session_id, caps)
        data_model = parse_client_data_model(metadata)
        if data_model is not None and not session_requested_send_data_model(app, session_id):
            raise A2UICapabilitiesError(
                "a2uiClientDataModel was sent but no live surface in this session "
                "requested sendDataModel",
                reason="a2ui_data_model_not_requested",
            )
    except A2UICapabilitiesError as exc:
        if registry is not None:
            registry.record_session_reason(session_id, exc.reason)
        raise
    return data_model


def client_capabilities(app: "FastAPI", session_id: str) -> A2UIClientCapabilities | None:
    """Return the session's last-remembered client capabilities, or ``None``."""

    session = app.state.sessions.get(session_id)
    if session is None:
        return None
    raw = (session.metadata or {}).get(A2UI_CLIENT_CAPABILITIES_METADATA_KEY)
    if not isinstance(raw, Mapping):
        return None
    try:
        return A2UIClientCapabilities.model_validate(raw)
    except _PydanticValidationError:
        # A value this module itself wrote can only fail to re-validate if the
        # stored session row was hand-edited/corrupted out of band -- treat it
        # as "no advertisement" (never crash a read path over it); the typed
        # a2ui_client_capabilities_unknown reason still fires at selection time.
        return None


def agent_capabilities(app: "FastAPI", session_id: str | None) -> dict[str, Any]:
    """Build the official ``{"v0.9": {supportedCatalogIds, acceptsInlineCatalogs}}``.

    Args:
        app: The FastAPI app carrying ``app.state.a2ui_catalogs``.
        session_id: A session to scope ``supportedCatalogIds`` to its
            PRODUCIBLE set (builtin ∪ active blueprint's declared catalogs),
            or ``None`` for the server-wide capability (every INSTALLED
            catalog, builtin ∪ every discovered pack) -- the shape
            ``GET /v1/capabilities`` (no session in scope) advertises.

    Returns:
        A plain dict (``by_alias=True`` dump) validated round-trip through
        :class:`A2UIAgentCapabilities` so a malformed id list can never reach
        the wire silently coerced.
    """

    if session_id is not None:
        ids = session_producible_catalog_ids(app, session_id)
    else:
        registry: "CatalogRegistry | None" = getattr(app.state, "a2ui_catalogs", None)
        ids = sorted({entry.catalog_id for entry in registry.installed()}) if registry else []
    model = A2UIAgentCapabilities.model_validate(
        {"v0.9": {"supportedCatalogIds": ids, "acceptsInlineCatalogs": False}}
    )
    return model.model_dump(mode="json", by_alias=True)


@dataclass(frozen=True)
class CatalogSelection:
    """The result of :func:`select_catalog` -- always returned, never raised.

    A producer tool (S4) turns an unsuccessful selection into a typed tool
    refusal; it is never silently defaulted to a catalog the client did not
    ask for.
    """

    catalog_id: str | None
    reason: str | None  # None on success
    client_supported_catalog_ids: tuple[str, ...] = field(default_factory=tuple)
    producible_catalog_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        """Whether a catalog was actually selected."""

        return self.catalog_id is not None


def select_catalog(
    app: "FastAPI", session_id: str, preferred: str | None = None
) -> CatalogSelection:
    """Select the first client-preferred catalog this session can produce.

    "The agent selects the best match from the client's ``supportedCatalogIds``
    list" (protocol) -- honours the CLIENT's preference order, gated to the
    session's producible set. ``preferred`` (e.g. an explicit tool argument)
    wins ONLY when it is itself in both sets; it never bypasses either. The
    choice is not persisted here -- "locked per surface" is a surface-record
    concern the producer tool (S4) owns at ``createSurface`` time.

    Every non-selection is a typed, recorded reason, never a silent default:

    * no client advertisement yet -> ``a2ui_client_capabilities_unknown``
    * a real advertisement with zero intersection against the session's
      producible set -> ``a2ui_catalog_no_client_match``
    """

    registry: "CatalogRegistry | None" = getattr(app.state, "a2ui_catalogs", None)
    caps = client_capabilities(app, session_id)
    producible = tuple(session_producible_catalog_ids(app, session_id))
    if caps is None:
        if registry is not None:
            registry.record_session_reason(session_id, "a2ui_client_capabilities_unknown")
        return CatalogSelection(
            catalog_id=None,
            reason="a2ui_client_capabilities_unknown",
            producible_catalog_ids=producible,
        )
    supported = tuple(caps.v0_9.supportedCatalogIds)
    producible_set = set(producible)
    if preferred is not None and preferred in supported and preferred in producible_set:
        return CatalogSelection(
            catalog_id=preferred,
            reason=None,
            client_supported_catalog_ids=supported,
            producible_catalog_ids=producible,
        )
    for catalog_id in supported:
        if catalog_id in producible_set:
            return CatalogSelection(
                catalog_id=catalog_id,
                reason=None,
                client_supported_catalog_ids=supported,
                producible_catalog_ids=producible,
            )
    if registry is not None:
        registry.record_session_reason(
            session_id,
            "a2ui_catalog_no_client_match",
            client_supported_catalog_ids=list(supported),
            producible_catalog_ids=list(producible),
        )
    return CatalogSelection(
        catalog_id=None,
        reason="a2ui_catalog_no_client_match",
        client_supported_catalog_ids=supported,
        producible_catalog_ids=producible,
    )


def strip_renderer_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return ``metadata`` with every renderer transport-metadata key removed.

    The protocol's own rule for ``sendDataModel``: "sent exclusively to the
    server that created the surface"; "orchestrators MUST strip it before
    sub-agents." Applied at every site that hands a metadata mapping derived
    from a parent turn/session to a spawned child/expert turn (see the module
    docstring). A no-op (returns ``{}``) for an absent/empty mapping.
    """

    if not metadata:
        return {}
    return {key: value for key, value in metadata.items() if key not in RENDERER_METADATA_KEYS}


def blueprint_a2ui_capability_ids(app: "FastAPI", agent_blueprint_id: str) -> list[str]:
    """Return one blueprint's declared catalog ids, unioned with the builtins.

    Used by the agent-listing rows (``routes/agents.py``): each row's
    ``a2ui_capabilities`` names what ITS OWN blueprint declares, not the
    caller session's active blueprint -- a listing enumerates every agent,
    most of which are not the session's currently active one.
    """

    registry: "CatalogRegistry | None" = getattr(app.state, "a2ui_catalogs", None)
    if registry is None:
        return []
    ids = {entry.catalog_id for entry in registry.builtin()}
    if not agent_blueprint_id:
        return sorted(ids)
    from clio_agent.gact.a2ui_catalogs.blueprint import (  # noqa: PLC0415
        blueprint_catalog_map,
        load_blueprint_catalogs,
    )

    blueprint = next(
        (row for row in registry.discovered_blueprints() if row.id == agent_blueprint_id),
        None,
    )
    if blueprint is not None and blueprint_catalog_map(blueprint):
        ids.update(entry.catalog_id for entry in load_blueprint_catalogs(blueprint))
    return sorted(ids)


__all__ = [
    "A2UI_CLIENT_CAPABILITIES_METADATA_KEY",
    "A2UI_CLIENT_CAPABILITIES_WIRE_KEY",
    "A2UI_CLIENT_DATA_MODEL_METADATA_KEY",
    "A2UI_CLIENT_DATA_MODEL_WIRE_KEY",
    "RENDERER_METADATA_KEYS",
    "A2UICapabilitiesError",
    "CatalogSelection",
    "agent_capabilities",
    "apply_client_metadata_guards",
    "blueprint_a2ui_capability_ids",
    "client_capabilities",
    "parse_client_capabilities",
    "parse_client_data_model",
    "remember_client_capabilities",
    "select_catalog",
    "session_requested_send_data_model",
    "strip_renderer_metadata",
]
