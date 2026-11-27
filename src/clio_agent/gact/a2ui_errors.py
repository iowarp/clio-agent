"""The typed errors of the A2UI surface fold, and the surface owner check.

Each error is an :class:`A2UIValidationError` a producer can act on: a settled
transcript, an unknown or unproducible catalog, or a surface that another producer
owns (host semantics HS1). :mod:`clio_agent.gact.a2ui` re-exports all of them.
"""

from __future__ import annotations

from typing import Any, Mapping

from clio_agent.gact.a2ui_catalogs.validation import A2UIValidationError


class A2UITranscriptFrozenError(A2UIValidationError):
    """Raised when a settled transcript can no longer accept an A2UI part.

    Distinct from a payload rejection: the batch was valid, but the ledger it
    would land in is closed, so the producer is told ``transcript_frozen``
    rather than being handed a validation message it cannot act on.
    """


class A2UICatalogUnknownError(A2UIValidationError):
    """Raised when a ``catalogId`` does not resolve in the installed registry.

    Carries ``catalog_id`` so a caller can attach the typed
    ``a2ui_catalog_unknown`` reason (production) — or, on transcript replay
    (``project_a2ui_parts``), fold the surface to ``state="unknown"`` with
    ``a2ui_catalog_unavailable`` instead of quarantining it.
    """

    def __init__(self, catalog_id: str) -> None:
        self.catalog_id = catalog_id
        super().__init__(f"A2UI catalog is not known: {catalog_id or '<empty>'}")


class A2UICatalogNotProducibleError(A2UIValidationError):
    """Raised when a catalog is installed but not producible in this session."""

    def __init__(self, catalog_id: str) -> None:
        self.catalog_id = catalog_id
        super().__init__(f"A2UI catalog is not producible in this session: {catalog_id}")


class A2UISurfaceOwnerError(A2UIValidationError):
    """A batch touched a surface another producer owns (host semantics HS1).

    ``reason`` is ``a2ui_surface_owner_conflict`` for a ``createSurface`` over a
    live surface someone else owns, and ``a2ui_surface_not_owned`` otherwise.
    """

    def __init__(self, reason: str, surface_id: str) -> None:
        self.reason = reason
        self.surface_id = surface_id
        super().__init__(f"A2UI surface {surface_id} belongs to another producer ({reason})")


def owner_key(owner: Mapping[str, Any] | None) -> tuple[str, str, str] | None:
    """The identity of a surface owner: ``(kind, plugin, server)``; ``None`` for the agent."""

    if not owner:
        return None
    return (
        str(owner.get("kind") or ""),
        str(owner.get("plugin") or ""),
        str(owner.get("server") or ""),
    )


def check_surface_owner(
    surface_owner: Mapping[str, Any] | None,
    owner: Mapping[str, Any] | None,
    message: Mapping[str, Any],
    surface_id: str,
) -> None:
    """Refuse ``message`` on a live surface unless ``owner`` is the one that created it."""

    if owner_key(surface_owner) == owner_key(owner):
        return
    reason = (
        "a2ui_surface_owner_conflict" if "createSurface" in message else "a2ui_surface_not_owned"
    )
    raise A2UISurfaceOwnerError(reason, surface_id)


__all__ = [
    "A2UICatalogNotProducibleError",
    "A2UICatalogUnknownError",
    "A2UISurfaceOwnerError",
    "A2UITranscriptFrozenError",
    "check_surface_owner",
    "owner_key",
]
