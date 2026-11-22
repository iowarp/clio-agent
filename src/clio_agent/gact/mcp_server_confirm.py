"""Server-confirmed MCP calls: one ask, the server's own card (``docs/PERMISSIONS.md``).

A tool that declares ``io.iowarp/confirm`` (:mod:`clio_agent.tools.server_confirm`) asks
the person itself, with a card that carries what the host's yes/no cannot (a job plan,
its charge, held services). When CLIO would ask the person directly, it does not ask
too: it records the call as deferred to its server and flags the tools/call so the
server guarantees exactly one confirm before any side effect.

Deferral is only ever to a person-facing card. Unattended runs, the ai-review reviewer,
a decision only a person makes (``always_ask``), and any rule or mode that already
decides keep the host's own path. A flagged call that settles without asking, and
without saying it needed no ask, is a server bug: CLIO records
``mcp_server_confirm_missing`` and asks for that server's calls itself for the rest of
the session.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from clio_agent.gact.permission_delivery import record_resolved_permission
from clio_agent.tools import server_confirm

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

REASON_DEFERRED = "mcp_confirm_deferred_to_server"
REASON_MISSING = "mcp_server_confirm_missing"
REASON_UNDECLARED = "mcp_server_confirm_undeclared"
_TRACKED = 512


class ServerConfirm(str):
    """An ``"allow"`` that the tool's server, not CLIO, confirms with the person."""

    server_confirm = True

    def __new__(cls) -> ServerConfirm:
        return super().__new__(cls, "allow")


@dataclass
class Flagged:
    """A submitted call that carries the request flag, until it settles."""

    session_id: str
    server: str
    tool: str
    arguments: dict[str, Any]
    asked: bool = False


def _untrusted(app: Any) -> set[tuple[str, str]]:
    held = getattr(app.state, "mcp_server_confirm_untrusted", None)
    if held is None:
        held = app.state.mcp_server_confirm_untrusted = set()
    return held


def may_defer(
    app: FastAPI,
    sid: str,
    context: Mapping[str, Any] | None,
    approval_mode: str,
    policy_action: str,
) -> bool:
    """Whether a call CLIO is about to ask a person about goes to its server's card."""
    if not sid or not context or not context.get("server_confirm") or context.get("always_ask"):
        return False
    if approval_mode == "ai-review" and policy_action != "ask":
        return False  # the reviewer decides it, not a person
    return (sid, str(context.get("server") or "")) not in _untrusted(app)


def defer(app: FastAPI, sid: str, name: str, args: Mapping[str, Any], subject: str) -> str:
    record_resolved_permission(
        app,
        session_id=sid,
        tool_name=name,
        args=args,
        status="auto_approved",
        action="allow",
        summary=f"{subject} {name!r} is confirmed by its server's own ask",
        reason=REASON_DEFERRED,
    )
    return ServerConfirm()


def _stop_deferring(app: Any, flagged: Flagged, reason: str, summary: str) -> None:
    _untrusted(app).add((flagged.session_id, flagged.server))
    logger.warning(
        "Permission boundary reason=%s server=%s tool=%s", reason, flagged.server, flagged.tool
    )
    record_resolved_permission(
        app,
        session_id=flagged.session_id,
        tool_name=flagged.tool,
        args=flagged.arguments,
        status="auto_approved",
        action="allow",
        summary=summary,
        reason=reason,
    )


def undeclared(app: Any, flagged: Flagged) -> PermissionError:
    """The deferred call's server no longer declares its confirm: nothing is sent."""
    _stop_deferring(
        app,
        flagged,
        REASON_UNDECLARED,
        f"{flagged.tool!r} was not sent: {flagged.server!r} no longer confirms its own calls",
    )
    return PermissionError(
        f"{REASON_UNDECLARED}: {flagged.tool} was not sent because {flagged.server} no longer "
        "confirms its own calls. Call it again and CLIO will ask the person."
    )


class FlaggedCalls:
    """The in-flight flagged calls of one :class:`ExternalTasks` (bounded, in memory)."""

    def __init__(self) -> None:
        self._calls: OrderedDict[str, Flagged] = OrderedDict()

    def track(self, handle: str, flagged: Flagged) -> None:
        self._calls[handle] = flagged
        while len(self._calls) > _TRACKED:
            self._calls.popitem(last=False)

    def asked(self, handle: str) -> None:
        if (flagged := self._calls.get(handle)) is not None:
            flagged.asked = True

    def settled(self, app: Any, handle: str, *results: Any, failed: bool) -> None:
        """Check one flagged call's settlement against the server's promise."""
        flagged = self._calls.pop(handle, None)
        if flagged is None or flagged.asked:
            return
        if any(server_confirm.not_needed(result) for result in results):
            return
        if failed:  # a refused or failed call reports its own error; it is not settled work
            return
        _stop_deferring(
            app,
            flagged,
            REASON_MISSING,
            f"{flagged.tool!r} ran without its server's confirm; CLIO now asks for "
            f"{flagged.server!r} calls itself",
        )


def flag(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    """A tools/call ``_meta`` with the request flag added."""
    return {**(meta or {}), server_confirm.CONFIRM_KEY: server_confirm.REQUEST_FLAG}


__all__ = [
    "REASON_DEFERRED",
    "REASON_MISSING",
    "REASON_UNDECLARED",
    "Flagged",
    "FlaggedCalls",
    "ServerConfirm",
    "defer",
    "flag",
    "may_defer",
    "undeclared",
]
