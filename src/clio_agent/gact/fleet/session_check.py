"""Check a person's model choice against the requirements of the conversation's agent.

The person picks the model a conversation runs on; when that model lacks what the
agent's plugins require (image input for a PDF reader, say), sending is refused
with ``model_requirement_unmet``, the unmet requirements with the plugins that
need them, and the models that meet them. The same facts mark the picker's rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact.fleet.entries import FleetEntry
from clio_agent.gact.fleet.selection import unmet_requirements
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.sdk.model import Requires

if TYPE_CHECKING:
    from fastapi import FastAPI


def session_requirements(app: "FastAPI", sid: str) -> tuple[Requires, dict[str, list[str]]] | None:
    """The session agent's effective requirement and its attribution, or ``None``
    when the session has no resolvable agent (that is reported by its own gate)."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    try:
        resolution = session_resolution(app, sid)
    except (SessionAgentUnavailable, KeyError, ValueError):
        return None
    section: dict[str, Any] = dict(getattr(resolution, "model", None) or {})
    return Requires.from_dict(section.get("requires") or {}), dict(section.get("attribution") or {})


def entry_fit(
    entries: list[FleetEntry], requires: Requires, attribution: dict[str, list[str]]
) -> dict[str, dict[str, Any]]:
    """Per entry ref: whether it meets ``requires``, and what it misses or leaves unverified."""

    fit: dict[str, dict[str, Any]] = {}
    for entry in entries:
        unmet, unverified = unmet_requirements(entry, requires, attribution)
        fit[entry.ref] = {"meets": not unmet, "unmet": unmet, "unverified": unverified}
    return fit


def model_requirement_error(
    app: "FastAPI", provider_id: str, model_id: str, sid: str
) -> ErrorEnvelope | None:
    """A typed error when the chosen model lacks what the session's agent requires."""

    from clio_agent.gact.fleet.child_model import fleet_entries  # noqa: PLC0415

    found = session_requirements(app, sid)
    if found is None or not any(found[0].to_dict().values()):
        return None
    requires, attribution = found
    entries = fleet_entries(app, needed=False)
    entry = next((e for e in entries if e.account == provider_id and e.model == model_id), None)
    if entry is None:
        return None  # A model the fleet does not list is judged by the catalog-evidence gate.
    unmet, _unverified = unmet_requirements(entry, requires, attribution)
    if not unmet:
        return None
    fitting = sorted(
        e.ref
        for e in entries
        if e.available and not unmet_requirements(e, requires, attribution)[0]
    )
    return ErrorEnvelope(
        error=ErrorInfo(
            error="model_requirement_unmet",
            message=(
                f"{entry.ref} lacks {'; '.join(unmet)}. Choose a model that meets this "
                f"agent's requirements: {', '.join(fitting) or 'none is connected'}."
            ),
            details={"model": entry.ref, "unmet": unmet, "fitting": fitting, "session_id": sid},
            recoverable=True,
        )
    )


__all__ = ["entry_fit", "model_requirement_error", "session_requirements"]
