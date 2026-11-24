"""One consent for a server's view-only tools when a person acts on its surface.

A person acting on a server-owned surface (a plugin page, a plugin tab, or a server's
surface in a conversation) makes that server run its view-only tools: tools whose
``_meta.ui.visibility`` lets apps call them and leaves the model out. A surface can use
many of them (open a form, check it, save it), and each one used to raise its own
external MCP question, worded with the tool's raw name and arguments.

The model can never call those tools, so the person is answering one question: may this
server act on what I do on its surface? While a surface action runs, :func:`scope`
carries a :class:`SurfaceConsent` naming the server's view-only tools and a readable
summary. The permission gate words the question with it and records the tools on the
row; an "allow for this session" or "allow for this workspace" answer grants every one
of them, so the next action on any of the server's controls runs without asking again.
A tool the model can also see keeps its own per-tool question. On the plugin's own page
(:data:`PLACE_PAGE`) the click itself is the consent (:func:`page_click`): no question
is raised for its app-only tools. Nothing here names a particular server.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

#: Where a surface lives, as the question words it.
PLACE_PAGE = "page"
PLACE_TAB = "tab"
PLACE_CONVERSATION = "conversation"


@dataclass(frozen=True)
class SurfaceConsent:
    """The view-only tools one server's surface may run, and how to ask about them."""

    namespace: str
    server_title: str
    place: str
    place_title: str
    tools: tuple[str, ...]

    @property
    def summary(self) -> str:
        if self.place == PLACE_CONVERSATION or not self.place_title:
            return f"Let {self.server_title} act on what you do in its view"
        return f"Let {self.server_title} act on what you do in its {self.place_title} {self.place}"

    def wire(self) -> dict[str, Any]:
        return {
            "scope": "server_surface",
            "namespace": self.namespace,
            "server_title": self.server_title,
            "place": self.place,
            "place_title": self.place_title,
            "tools": list(self.tools),
        }


_CONSENT: ContextVar[SurfaceConsent | None] = ContextVar("clio_surface_consent", default=None)


def _listed_name(tool: Any) -> str:
    name = tool.get("name") if isinstance(tool, Mapping) else getattr(tool, "name", "")
    return str(name or "")


def view_only_tools(namespace: str, listed: Iterable[Any]) -> tuple[str, ...]:
    """``<namespace>_<tool>`` for each listed tool an app may call and the model may not."""

    from clio_agent.gact.mcp_apps import _tool_visible_to_app  # noqa: PLC0415
    from clio_agent.tools.tool_ui_metadata import _tool_visible_to_model  # noqa: PLC0415

    names: list[str] = []
    for tool in listed:
        bare = _listed_name(tool)
        full = f"{namespace}_{bare}"
        if bare and _tool_visible_to_app(tool) and not _tool_visible_to_model(full, tool):
            names.append(full)
    return tuple(dict.fromkeys(names))


def for_surface(
    namespace: str,
    server_title: str,
    listed: Iterable[Any],
    *,
    place: str = PLACE_CONVERSATION,
    place_title: str = "",
) -> SurfaceConsent | None:
    """The consent for one server's surface, or ``None`` when it has no view-only tools."""

    tools = view_only_tools(namespace, listed)
    if not tools:
        return None
    return SurfaceConsent(
        namespace=namespace,
        server_title=server_title or namespace,
        place=place,
        place_title=place_title,
        tools=tools,
    )


@contextmanager
def scope(consent: SurfaceConsent | None) -> Iterator[None]:
    """Ask about ``consent`` for the view-only calls made inside this block."""

    token = _CONSENT.set(consent)
    try:
        yield
    finally:
        _CONSENT.reset(token)


def covering(tool_name: str) -> SurfaceConsent | None:
    """The active consent when it covers ``tool_name``, else ``None``."""

    consent = _CONSENT.get()
    return consent if consent is not None and tool_name in consent.tools else None


#: The permission record's reason when a click on the server's own page allowed the call.
REASON_PAGE_CLICK = "surface_page_click"


def page_click(tool_name: str) -> SurfaceConsent | None:
    """The active consent when a click on its server's own page runs ``tool_name``.

    A plugin's page (:data:`PLACE_PAGE`) is the plugin's own place, opened by the person
    from CLIO's navigation; a click on one of its controls is the person asking that
    plugin to act. It is consent for the plugin's app-only tools there (the ones
    :func:`view_only_tools` lists), so no further question is raised. A tab or a
    conversation surface, and any tool the model can also call, still ask.
    """

    consent = covering(tool_name)
    return consent if consent is not None and consent.place == PLACE_PAGE else None


def granted_tools(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The tools a permission row's surface consent covers, else ``()``."""

    consent = row.get("consent")
    if not isinstance(consent, Mapping) or consent.get("scope") != "server_surface":
        return ()
    tools = consent.get("tools")
    if not isinstance(tools, list):
        return ()
    return tuple(str(tool) for tool in tools if isinstance(tool, str) and tool)


__all__ = [
    "PLACE_CONVERSATION",
    "PLACE_PAGE",
    "PLACE_TAB",
    "REASON_PAGE_CLICK",
    "SurfaceConsent",
    "covering",
    "for_surface",
    "granted_tools",
    "page_click",
    "scope",
    "view_only_tools",
]
