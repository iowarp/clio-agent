"""Routes for the plugins the agent may offer (:mod:`clio_agent.gact.offer_list`).

* ``GET /v1/plugins/offer-list`` every candidate for a new conversation with the
  person's default agent, in order: ``{key, kind, id, name, summary, logo_url,
  logo_dark_url, default_listed, listed}`` (plus ``marketplace`` for a plugin and
  ``enabled`` for a connector), with ``cap``, ``total`` and ``listed_count``.
* ``PUT /v1/plugins/offer-list`` ``{include: [key], exclude: [key]}`` sets the
  person's ticks over the default list (unknown keys are dropped); returns the list.
* ``DELETE /v1/plugins/offer-list`` resets to the default list; returns the list.

A refusal is the typed error envelope (``offer_list_*``, or the agent's resolution
reason when the default agent does not resolve).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact import offer_list
from clio_agent.gact.routes._body import json_body
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

ROUTE = "/v1/plugins/offer-list"


def _error(exc: offer_list.OfferListError) -> HTTPException:
    return HTTPException(
        status_code=exc.status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=exc.code, message=str(exc), recoverable=exc.status < 500)
        ).model_dump(exclude_none=True),
    )


def _strings(body: dict[str, Any], name: str) -> list[str]:
    value = body.get(name, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _error(
            offer_list.OfferListError("validation_error", f"{name} must be a list of keys")
        )
    return value


def register_offer_list_routes(app: FastAPI) -> None:
    """Register the offer-list routes."""

    @app.get(ROUTE)
    async def get_offer_list() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(offer_list.view, app)
        except offer_list.OfferListError as exc:
            raise _error(exc) from None

    @app.put(ROUTE)
    async def put_offer_list(request: Request) -> dict[str, Any]:
        body = await json_body(request, route=f"PUT {ROUTE}")
        include, exclude = _strings(body, "include"), _strings(body, "exclude")
        try:
            return await asyncio.to_thread(offer_list.update, app, include, exclude)
        except offer_list.OfferListError as exc:
            raise _error(exc) from None

    @app.delete(ROUTE)
    async def reset_offer_list() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(offer_list.reset, app)
        except offer_list.OfferListError as exc:
            raise _error(exc) from None
