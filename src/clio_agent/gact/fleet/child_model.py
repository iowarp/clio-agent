"""Choose a child session's model from its parent, its resolution and the caller's ask."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact.fleet.entries import FleetEntry, project_fleet
from clio_agent.gact.fleet.selection import ModelChoice, ModelSelectionRefused, choose_model
from clio_agent.sdk.model import Requires

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.sessions import Session
    from clio_agent.plugins.resolve import Resolution



def fleet_entries(app: "FastAPI", *, needed: bool) -> list[FleetEntry]:
    """The fleet's entries from the provider catalog the server last read.

    Raises:
        ModelSelectionRefused: ``model_catalog_not_read`` when a choice needs the
            fleet and the server has not read its provider catalog yet.
    """

    catalog = getattr(app.state, "provider_catalog", None)
    if not isinstance(catalog, dict):
        if needed:
            raise ModelSelectionRefused(
                "model_catalog_not_read",
                "the server has not read its provider catalog yet; retry in a moment",
            )
        return []
    return project_fleet(catalog)[1]


def caller_model(app: "FastAPI", parent: "Session") -> dict[str, str]:
    """The model the caller runs on: its pinned model, else the server's active one."""

    model = dict(parent.model or {})
    if model.get("provider_id") and model.get("model_id"):
        return {"provider_id": model["provider_id"], "model_id": model["model_id"]}
    from clio_agent.gact.providers.config import _active_lm_model_ref  # noqa: PLC0415

    active = _active_lm_model_ref(app)
    return {"provider_id": active["provider_id"], "model_id": active["model_id"]}


def choose_child_model(
    app: "FastAPI",
    parent: "Session",
    child: "Resolution",
    *,
    explicit: str,
    same_agent: bool,
) -> ModelChoice:
    """The child's model choice; ``same_agent`` (a flow run of the caller's own agent)
    skips preferences, since the caller already runs that agent."""

    section: dict[str, Any] = dict(getattr(child, "model", None) or {})
    chain = [] if same_agent else list(section.get("prefers_chain") or [])
    return choose_model(
        fleet_entries(app, needed=bool(explicit.strip() or chain or section.get("requires"))),
        explicit=explicit,
        prefers_chain=chain,
        requires=Requires.from_dict(section.get("requires") or {}),
        attribution=dict(section.get("attribution") or {}),
        inherited=caller_model(app, parent),
    )


def accepted_model(app: "FastAPI", accepted: dict[str, Any]) -> dict[str, Any]:
    """``{"model": <model_choice>}`` for an accepted spawn, so the caller sees which
    model its child runs on and why; empty when nothing was accepted."""

    from clio_agent.gact.fleet.selection import MODEL_CHOICE_KEY  # noqa: PLC0415

    session = app.state.sessions.get(str(accepted.get("session_id") or ""))
    choice = (getattr(session, "metadata", None) or {}).get(MODEL_CHOICE_KEY)
    return {"model": choice} if choice else {}


async def warm_model_catalog(app: "FastAPI") -> None:
    """Read the provider catalog once if the server has not yet, so a child's model
    can be checked. A provider that cannot be reached is a row of the catalog, not
    an error; anything else that fails the read fails the spawn with its cause."""

    if isinstance(getattr(app.state, "provider_catalog", None), dict):
        return
    from clio_agent.gact.provider_catalog_snapshot import read_catalog  # noqa: PLC0415

    await read_catalog(app)


__all__ = [
    "accepted_model",
    "caller_model",
    "choose_child_model",
    "fleet_entries",
    "warm_model_catalog",
]
