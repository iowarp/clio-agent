"""Web search routes: the person's sources, their keys, and the CLIO search service API.

Settings (Settings → Web search):

* ``GET /v1/search/providers`` -- every web and literature source in order, with
  whether it is on, approved, has a key, its access tier (no key, contact email,
  free key, paid key), how much of the free allowance this CLIO used, how many
  requests wait in CLIO's queue, whether it is resting (``limited_until``), and how
  often it answered or failed today.
* ``PATCH /v1/search/providers`` -- change order, toggles, approvals, automatic
  switching, the service URL, the contact email, or which keys are on a paid plan.
* ``PUT``/``DELETE /v1/search/providers/{id}/key`` -- keep a source's key in CLIO's
  credential store (never returned, logged or written to config). Adding a key
  puts the source first unless ``placement`` is ``keep``.
* ``POST /v1/search/openalex-key`` -- ask OpenAlex for a free key for the person's
  email; it works after they click the emailed link.

Service API (another CLIO's ``service`` source calls these; bearer auth applies):

* ``POST /v1/search`` and ``POST /v1/fetch`` answer like ``web_search``/``web_fetch``.
* ``GET /v1/limits`` reports each source's state.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from clio_agent.gact.routes._body import json_body
from clio_agent.providers.api_key_store import ProviderApiKeyStore
from clio_agent.search import broker, http
from clio_agent.search.catalog import LITERATURE_INFO, WEB_INFO
from clio_agent.search.fetch import web_fetch
from clio_agent.search.scheduler import SCHEDULER
from clio_agent.search.settings import (
    LITERATURE_APPROVAL,
    LITERATURE_SOURCES,
    SERVICE_SOURCE,
    WEB_VENDORS,
    SearchConfigurationError,
    SearchSettings,
    key_ref,
    load_search_settings,
)
from clio_agent.search.settings_store import update_search_settings
from clio_agent.search.terms import TIER_WORDS, terms_for, tier_of
from clio_agent.search.web import SearchFailure, web_search
from clio_agent.search.web_sources import WebQuery
from clio_agent.tools.servers.web_limits import SEARCH_LIMITS

_KNOWN = (*WEB_VENDORS, SERVICE_SOURCE, *LITERATURE_SOURCES)
_OPENALEX_KEYS = "https://api.openalex.org/api-keys"


def _has_key(source: str) -> bool:
    # load() reads an unreadable or mismatched store as "no key"; it does not raise.
    return bool(ProviderApiKeyStore().load(key_ref(source)))


def _row(
    info: dict[str, Any], settings: SearchSettings, status: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    source = str(info["id"])
    state = status.get(source, {})
    keyed = _has_key(source)
    tier = tier_of(source, settings, has_key=keyed)
    queue = SCHEDULER.view(source, terms_for(source, tier))
    return {
        **info,
        **extra,
        "has_key": keyed,
        "paid": source in settings.paid,
        "tier": tier,
        "tier_words": TIER_WORDS[tier],
        "allowance": queue["used"],
        "waiting": queue["waiting"],
        "running": queue["running"],
        "declined": SEARCH_LIMITS.declined(source),
        "limited_until": state.get("limited_until"),
        "limited_kind": state.get("limited_kind"),
        "last_answered": state.get("last_answered"),
        "last_failure": state.get("last_failure"),
        "last_failure_kind": state.get("last_failure_kind"),
        "answered_today": state.get("answered_today", 0),
        "failed_today": state.get("failed_today", 0),
    }


def providers_view(settings: SearchSettings) -> dict[str, Any]:
    """The settings view: both source lists with live state."""

    status = SEARCH_LIMITS.status()
    order = [name for name in settings.web_order if name != SERVICE_SOURCE]
    if settings.service_url:
        order = [SERVICE_SOURCE, *order]
    web = [
        _row(
            WEB_INFO[name].as_dict(),
            settings,
            status,
            position=index,
            enabled=name not in settings.web_disabled,
            approved=settings.is_approved(name),
        )
        for index, name in enumerate(order)
    ]
    literature = [
        _row(
            LITERATURE_INFO[name].as_dict(),
            settings,
            status,
            enabled=name not in settings.literature_disabled,
        )
        for name in LITERATURE_SOURCES
    ]
    return {
        "enabled": settings.enabled,
        "auto_switch": settings.auto_switch,
        "service_url": settings.service_url,
        "contact_email": settings.contact_email,
        "literature_approved": settings.is_approved(LITERATURE_APPROVAL),
        "web": web,
        "literature": literature,
    }


def _source(name: str) -> str:
    source = name.strip().casefold()
    if source not in _KNOWN:
        raise HTTPException(status_code=404, detail=f"unknown search source {name!r}")
    return source


def _apply_patch(body: dict[str, Any]) -> SearchSettings:
    def change(section: dict[str, Any]) -> None:
        web = web_value if isinstance(web_value := section.get("web"), dict) else {}
        section["web"] = web
        literature = (
            literature_value
            if isinstance(literature_value := section.get("literature"), dict)
            else {}
        )
        section["literature"] = literature
        if "enabled" in body:
            section["enabled"] = bool(body["enabled"])
        if "web_order" in body:
            web["order"] = [str(x) for x in body["web_order"]]
        if "web_disabled" in body:
            web["disabled"] = [str(x) for x in body["web_disabled"]]
        if "auto_switch" in body:
            web["auto_switch"] = bool(body["auto_switch"])
        if "literature_disabled" in body:
            literature["disabled"] = [str(x) for x in body["literature_disabled"]]
        if "approved" in body:
            section["approved"] = [str(x) for x in body["approved"]]
        if "service_url" in body:
            section.setdefault("service", {})["url"] = str(body["service_url"] or "")
        if "paid" in body:
            section["paid"] = [str(x) for x in body["paid"]]
        if "contact_email" in body:
            section["contact_email"] = str(body["contact_email"] or "")

    return update_search_settings(change)


def register_search_routes(app: FastAPI) -> None:
    """Register the search settings and service routes."""

    @app.get("/v1/search/providers")
    async def search_providers() -> dict[str, Any]:
        try:
            settings = await asyncio.to_thread(load_search_settings)
        except SearchConfigurationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await asyncio.to_thread(providers_view, settings)

    @app.patch("/v1/search/providers")
    async def update_providers(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="PATCH /v1/search/providers")
        try:
            settings = await asyncio.to_thread(_apply_patch, body)
        except SearchConfigurationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await asyncio.to_thread(providers_view, settings)

    @app.put("/v1/search/providers/{source}/key")
    async def save_source_key(source: str, request: Request) -> dict[str, Any]:
        name = _source(source)
        body = await json_body(request, route="PUT /v1/search/providers/{source}/key")
        key = str(body.get("api_key") or "").strip()
        if not key:
            raise HTTPException(status_code=422, detail="api_key is required")
        await asyncio.to_thread(ProviderApiKeyStore().save, key_ref(name), key)
        SEARCH_LIMITS.clear(name)
        settings = await asyncio.to_thread(load_search_settings)
        if name in WEB_VENDORS and str(body.get("placement") or "first") == "first":
            order = [name, *[n for n in settings.web_order if n != name]]
            settings = await asyncio.to_thread(_apply_patch, {"web_order": order})
        return {"source": name, "saved": True, "settings": providers_view(settings)}

    @app.delete("/v1/search/providers/{source}/key")
    async def clear_source_key(source: str) -> dict[str, Any]:
        name = _source(source)
        removed = await asyncio.to_thread(ProviderApiKeyStore().clear, key_ref(name))
        return {"source": name, "removed": removed}

    @app.post("/v1/search/openalex-key")
    async def request_openalex_key(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/search/openalex-key")
        email = str(body.get("email") or "").strip()
        if "@" not in email or " " in email:
            raise HTTPException(status_code=422, detail="email must be the person's email address")
        try:
            response = await http.request("openalex", "POST", _OPENALEX_KEYS, json={"email": email})
            answer = http.json_body("openalex", response)
        except http.SourceError as exc:
            raise HTTPException(
                status_code=502, detail=f"OpenAlex did not issue a key: {exc}"
            ) from exc
        key = str(answer.get("api_key") or "") if isinstance(answer, dict) else ""
        if key:
            await asyncio.to_thread(ProviderApiKeyStore().save, key_ref("openalex"), key)
        return {
            "saved": bool(key),
            "status": answer.get("status") if isinstance(answer, dict) else None,
            "message": answer.get("message") if isinstance(answer, dict) else None,
        }

    # ------------------------------------------------------------ service API

    @app.post("/v1/search")
    async def service_search(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/search")
        query = WebQuery(
            query=str(body.get("query") or ""),
            after=str(body.get("after") or ""),
            before=str(body.get("before") or ""),
            include_domains=[str(d) for d in body.get("include_domains") or []],
            exclude_domains=[str(d) for d in body.get("exclude_domains") or []],
            kind=str(body.get("kind") or "any"),
            limit=int(body.get("limit") or 8),
        )
        try:
            return await web_search(query)
        except (broker.AskPerson, broker.WaitingForPerson) as exc:
            raise HTTPException(
                status_code=409,
                detail="this search service needs its operator to approve its sources in Settings",
            ) from exc
        except SearchFailure as failure:
            raise HTTPException(status_code=503, detail=failure.as_result()) from failure
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/fetch")
    async def service_fetch(request: Request) -> dict[str, Any]:
        body = await json_body(request, route="POST /v1/fetch")
        url = str(body.get("url") or "")
        if not url:
            raise HTTPException(status_code=422, detail="url is required")
        return await web_fetch(url, int(body.get("max_chars") or 400_000))

    @app.get("/v1/limits")
    async def service_limits() -> dict[str, Any]:
        settings = load_search_settings()
        rows = SEARCH_LIMITS.status()
        for name, row in rows.items():
            tier = tier_of(name, settings, has_key=_has_key(name))
            row.update(tier=tier, **SCHEDULER.view(name, terms_for(name, tier)))
        return {"sources": rows}


__all__ = ["providers_view", "register_search_routes"]
