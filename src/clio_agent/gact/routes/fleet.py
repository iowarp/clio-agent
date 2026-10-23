"""``GET /v1/fleet``: connected accounts, their usage and spend, and their models."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.fleet.child_model import caller_model
from clio_agent.gact.fleet.guard import set_delegation_allowed
from clio_agent.gact.fleet.read import read_fleet
from clio_agent.gact.fleet.session_check import entry_fit, session_requirements
from clio_agent.gact.fleet.spend import tree_spend
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo


def register_fleet_routes(app: FastAPI) -> None:
    """Register the fleet read route."""

    @app.get("/v1/fleet")
    async def fleet(refresh: bool = False, session: str = "") -> dict[str, Any]:
        """The fleet: ``refresh=true`` re-probes providers and re-reads polled usage.

        ``session`` marks each entry with whether it meets that conversation agent's
        requirements (``fit``), for the model picker.
        """

        reading = await read_fleet(app, refresh=refresh)
        wire = reading.to_wire()
        found = session_requirements(app, session) if session else None
        stored = app.state.sessions.get(session) if session else None
        if stored is not None:
            # The account the conversation runs on, and what it and the work it
            # started spent per model: the readout under the chat box and a run's
            # railroad summary.
            wire["session"] = {
                "id": session,
                "model": caller_model(app, stored),
                "spend_by_model": {
                    ref: s.to_dict() for ref, s in tree_spend(app, session).by_model.items()
                },
            }
        if found is not None:
            fit = entry_fit(reading.entries, *found)
            wire["requires"] = found[0].to_dict()
            for row in wire["entries"]:
                row["fit"] = fit.get(row["ref"])
        return wire

    @app.patch("/v1/fleet/accounts/{account}")
    async def fleet_account(account: str, request: Request) -> dict[str, Any]:
        """Set whether Clio may run delegated work on ``account`` without asking."""

        body = await request.json()
        allowed = body.get("delegation_allowed") if isinstance(body, dict) else None
        if not isinstance(allowed, bool):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="invalid_request",
                        message="delegation_allowed must be true or false",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            set_delegation_allowed(account, allowed)
        except ValueError as exc:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(error="not_found", message=str(exc), recoverable=False)
                ).model_dump(exclude_none=True),
            ) from exc
        return {"account": account, "delegation_allowed": allowed}


__all__ = ["register_fleet_routes"]
