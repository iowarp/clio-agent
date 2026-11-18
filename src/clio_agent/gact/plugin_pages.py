"""Plugin pages: a plugin's page in the host's primary navigation (host semantics HS3).

A plugin declares one ``navigation`` page in its ``ai.iowarp.clio`` extension: a tab's
fields, shown in the host's left bar instead of beside a conversation. A page is
global. Its host-owned context is a hidden session of kind ``page`` (one per page, part
of no conversation) whose agent mounts the plugin, and the page is that context's tab.
So the resource watch, revisions, actions, the first-click assignment anchor, permission
prompts and the plugin update remount are the tab machinery's
(:mod:`~clio_agent.gact.plugin_tabs`). A page's events are ``plugin_page.*``, keyed by
``page_id``, on its context's event stream. Nothing here names a particular plugin.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from clio_agent.gact.plugin_tabs import PluginTabError, plugin_tabs

#: Session metadata marking a page's context: ``{"plugin", "page"}``.
PAGE_METADATA_KEY = "plugin_page"
#: The workspace a page's context lives in: pages are global, not a workspace's.
PAGE_WORKSPACE = "ws_default"
PLUGIN_NOT_TRUSTED = "plugin_not_trusted"
_TAB_EVENTS = "plugin_tab."
_PAGE_EVENTS = "plugin_page."


def page_of(session: Any) -> tuple[str, str] | None:
    """``(plugin, page)`` when ``session`` is a page's context, else ``None``."""

    metadata = getattr(session, "metadata", None)
    raw = metadata.get(PAGE_METADATA_KEY) if isinstance(metadata, Mapping) else None
    if isinstance(raw, Mapping) and raw.get("plugin") and raw.get("page"):
        return str(raw["plugin"]), str(raw["page"])
    return None


def page_context(app: Any, session_id: str) -> tuple[str, str] | None:
    """``(plugin, page)`` of the page whose context ``session_id`` is, else ``None``."""

    sessions = getattr(getattr(app, "state", None), "sessions", None)
    session = sessions.get(session_id) if sessions is not None and session_id else None
    return page_of(session) if session is not None else None


def page_event(
    app: Any, session_id: str, event_type: str, payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """A tab event of a page's context as the page's: ``plugin_page.*`` with ``page_id``."""

    if not event_type.startswith(_TAB_EVENTS):
        return event_type, payload
    page = page_context(app, session_id)
    if page is None or payload.get("tab_id") != f"{page[0]}/{page[1]}":
        return event_type, payload
    renamed = {("page_id" if key == "tab_id" else key): value for key, value in payload.items()}
    return _PAGE_EVENTS + event_type[len(_TAB_EVENTS) :], renamed


def declared_pages(app: Any) -> list[dict[str, Any]]:
    """The navigation page of every installed plugin declaring one, in catalog order."""

    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415

    service = plugin_service(app)
    service.ready()
    store = service.store
    pages: dict[str, dict[str, Any]] = {}
    for record in store.records():
        host = record.host or {}
        navigation = host.get("navigation")
        if not navigation or record.id in pages:
            continue
        trusted = store.trusted(record)
        row: dict[str, Any] = {
            "id": f"{record.id}/{navigation['id']}",
            "plugin": record.id,
            "page": str(navigation["id"]),
            "title": str(navigation["title"]),
            "icon": str(navigation.get("icon") or ""),
            "server": str(navigation["server"]),
            "resource": str(navigation["resource"]),
            "placement": "primary",
            "available": trusted,
            "reason": "" if trusted else PLUGIN_NOT_TRUSTED,
        }
        if host.get("title"):
            row["plugin_title"] = str(host["title"])
        pages[record.id] = row
    return list(pages.values())


def page_contexts(app: Any) -> dict[tuple[str, str], str]:
    """``(plugin, page) -> context session id`` of every live page context."""

    from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415

    query = PageQuery(archived=False, kinds=frozenset({"page"}), limit=1000)
    out: dict[tuple[str, str], str] = {}
    for session in app.state.sessions.select(query):
        page = page_of(session)
        if page is not None and page not in out:
            out[page] = session.id
    return out


def _default_model(app: Any) -> dict[str, str] | None:
    defaults = app.state.session_defaults.get()
    if not (defaults.provider_id and defaults.model_id):
        return None
    return {"provider_id": defaults.provider_id, "model_id": defaults.model_id}


def _adopt_default_model(app: Any, sid: str) -> None:
    """Give a context made before any model was chosen the default chosen since.

    The context has no conversation, so nothing else ever names its model, and
    the host that reads the page is built from that model.
    """

    session = app.state.sessions.get(sid)
    if session is None or getattr(session, "model", None):
        return
    if model := _default_model(app):
        app.state.sessions.update(sid, model=model)


def _create_context(app: Any, row: Mapping[str, Any]) -> str:
    """Create the hidden session a page lives in: the default agent plus the plugin."""

    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_DELTA_KEY,
        prepare_session_agent,
    )
    from clio_agent.gact.session_defaults import apply_default_effort  # noqa: PLC0415

    defaults = app.state.session_defaults.get()
    metadata: dict[str, Any] = {
        PAGE_METADATA_KEY: {"plugin": row["plugin"], "page": row["page"]},
        # Adding a plugin the agent already has changes nothing.
        AGENT_DELTA_KEY: {"add": [row["plugin"]], "remove": []},
    }
    apply_default_effort(metadata, defaults)
    resolution = prepare_session_agent(app, metadata)
    session = app.state.sessions.create(
        workspace_id=PAGE_WORKSPACE,
        title=str(row["title"]),
        metadata=metadata,
        model=_default_model(app),
        agent={"id": resolution.agent["id"]},
        mode=defaults.mode,
        edit_mode=defaults.edit_mode,
        routing_mode=defaults.routing_mode,
        approval_mode=defaults.approval_mode,
    )
    return str(session.id)


def _lock(app: Any) -> asyncio.Lock:
    lock = getattr(app.state, "plugin_pages_lock", None)
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        app.state.plugin_pages_lock = lock
    return lock


class PluginPages:
    """The app's navigation pages, each opened in its own host-owned context."""

    def __init__(self, app: Any) -> None:
        self._app = app

    async def list(self) -> list[dict[str, Any]]:
        from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

        pages = await run_off_loop(declared_pages, self._app)
        contexts = await run_off_loop(page_contexts, self._app)
        tabs = plugin_tabs(self._app)
        for row in pages:
            context = contexts.get((row["plugin"], row["page"]))
            row["context_session_id"] = context
            failure = tabs.failure(context, row["plugin"], row["page"]) if context else ""
            if row["available"] and failure:
                row["available"], row["reason"] = False, failure
        return pages

    async def _row(self, plugin: str, page: str) -> dict[str, Any]:
        from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

        for row in await run_off_loop(declared_pages, self._app):
            if row["plugin"] == plugin and row["page"] == page:
                if not row["available"]:
                    raise PluginTabError(409, row["reason"], f"plugin {plugin} is not trusted")
                return row
        raise PluginTabError(404, "plugin_page_not_found", f"no page {plugin}/{page}")

    async def context(self, plugin: str, page: str, *, create: bool) -> str:
        """The page's context session id; created on first open when ``create``."""

        from clio_agent.gact import session_warmup  # noqa: PLC0415
        from clio_agent.gact.agents.resolution import SessionAgentUnavailable  # noqa: PLC0415
        from clio_agent.gact.off_loop import run_off_loop  # noqa: PLC0415

        row = await self._row(plugin, page)
        async with _lock(self._app):
            found = (await run_off_loop(page_contexts, self._app)).get((plugin, page))
            if found or not create:
                if not found:
                    raise PluginTabError(
                        404, "plugin_page_not_open", f"page {plugin}/{page} is not open"
                    )
                await run_off_loop(_adopt_default_model, self._app, found)
                return found
            try:
                context = await run_off_loop(_create_context, self._app, row)
            except SessionAgentUnavailable as exc:
                raise PluginTabError(422, exc.reason, str(exc)) from exc
        # Start the plugin's servers now, as for any new session.
        session_warmup.start_session_warmup(self._app, context, trigger="session_created")
        return context

    async def open(self, plugin: str, page: str) -> dict[str, Any]:
        context = await self.context(plugin, page, create=True)
        opened = await plugin_tabs(self._app).open(context, plugin, page)
        tab = dict(opened["tab"])
        row = {**tab, "page": page, "context_session_id": context}
        return {
            "page": row,
            "context_session_id": context,
            "surfaces": opened["surfaces"],
            "revision": opened["revision"],
        }

    async def close(self, plugin: str, page: str) -> dict[str, Any]:
        try:
            context = await self.context(plugin, page, create=False)
        except PluginTabError:
            return {"closed": True}
        return await plugin_tabs(self._app).close(context, plugin, page)

    async def action(self, plugin: str, page: str, body: Mapping[str, Any]) -> dict[str, Any]:
        context = await self.context(plugin, page, create=False)
        return await plugin_tabs(self._app).action(context, plugin, page, body)


def plugin_pages(app: Any) -> PluginPages:
    """The app's page registry, created on first use."""

    pages = getattr(app.state, "plugin_pages", None)
    if not isinstance(pages, PluginPages):
        pages = PluginPages(app)
        app.state.plugin_pages = pages
    return pages


__all__ = [
    "PAGE_METADATA_KEY",
    "PAGE_WORKSPACE",
    "PLUGIN_NOT_TRUSTED",
    "PluginPages",
    "declared_pages",
    "page_context",
    "page_event",
    "page_of",
    "page_contexts",
    "plugin_pages",
]
