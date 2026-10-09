"""Can this deployment arm SPOTTER? The answer a mode picker needs BEFORE the click.

:mod:`clio_agent.gact.spotter_arming` refuses a transition into ``spotter-ai``
with a typed 422 when the watcher could not execute. That is the right barrier,
but it is only reachable by trying: a UI that offers the mode unconditionally
lets the operator pick it, then shows an error. This module answers the same
question up front, from the SAME validator, so the option is disabled with the
typed reason and what to enable instead of failing after the click.

One extra state is reported here that arming itself does not refuse: the
watcher Agent Blueprint is not installed. Arming then mints a watcher bound to
an id no registry resolves (typed ``installed_blueprint_id_unresolved`` at its
first turn); a picker must not advertise surveillance that has no watcher, so
it is reported as unavailable with the install remedy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.runtime import trace

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.spotter_mount_outcome import MountFailure

logger = logging.getLogger(__name__)

#: The configured watcher Agent Blueprint is not installed anywhere discovery scans.
UNAVAILABLE_BLUEPRINT_NOT_INSTALLED = "spotter_watcher_blueprint_not_installed"

#: Declarations resolve, but the watcher's MCP server failed to start on its
#: last use in this process (recorded by ``mcp_readiness``; cleared on success).
UNAVAILABLE_WATCHER_MCP_START_FAILED = "spotter_watcher_mcp_start_failed"

_NOT_INSTALLED_MESSAGE = "SPOTTER needs its watcher Agent Blueprint, which is not installed."


@dataclass(frozen=True)
class SpotterAvailability:
    """Whether ``spotter-ai`` can be armed, and if not, why and what to enable."""

    available: bool
    reason: str = ""
    message: str = ""
    remedy: str = ""
    blueprint_id: str = ""
    details: Optional[dict[str, str]] = None

    def to_wire(self) -> dict[str, Any]:
        """The ``GET /v1/spotter/availability`` payload."""

        return {
            "schema_version": "clio.spotter_availability.v1",
            "approval_mode": "spotter-ai",
            "available": self.available,
            "reason": self.reason,
            "message": self.message,
            "remedy": self.remedy,
            "agent_blueprint_id": self.blueprint_id,
            "details": dict(self.details or {}),
        }


def _blueprint_installed(
    app: "FastAPI", blueprint_id: str, *, session_id: str, workspace_id: str
) -> bool:
    from clio_agent.gact.agent_blueprints import discover_agent_blueprints  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import _runtime_workspace_catalog_cwd  # noqa: PLC0415

    cwd = _runtime_workspace_catalog_cwd(app, workspace_id=workspace_id, session_id=session_id)
    return any(row.id == blueprint_id for row in discover_agent_blueprints(cwd=cwd))


def _watcher_mount_failure(
    app: "FastAPI", blueprint_id: str, *, session_id: str, workspace_id: str
) -> Optional["MountFailure"]:
    """The watcher's declared server whose last runtime start failed, if any."""

    from clio_agent.gact.spotter_arming import _declared_watcher_servers  # noqa: PLC0415
    from clio_agent.gact.spotter_mount_outcome import last_failure  # noqa: PLC0415

    servers, _raw, _root = _declared_watcher_servers(
        app, blueprint_id, session_id=session_id, workspace_id=workspace_id
    )
    for name in servers:
        failure = last_failure(str(name))
        if failure is not None:
            return failure
    return None


def spotter_availability(
    app: "FastAPI", *, session_id: str = "", workspace_id: str = ""
) -> SpotterAvailability:
    """Statically decide whether ``spotter-ai`` can be armed for this scope.

    Args:
        app: The GACT app.
        session_id: The session the picker belongs to (its workspace decides
            the blueprint scan root and the provenance workspace root).
        workspace_id: Explicit workspace scope when there is no session yet.

    Returns:
        ``available=True``, or the first typed reason it is not, with the
        operator-facing message and remedy. Every unavailable answer is logged
        and traced with its reason.
    """

    from clio_agent.gact.spotter_arming import validate_watcher_arming  # noqa: PLC0415
    from clio_agent.gact.spotter_watcher import _watcher_blueprint_id  # noqa: PLC0415

    blueprint_id = _watcher_blueprint_id()
    result: SpotterAvailability
    if not _blueprint_installed(
        app, blueprint_id, session_id=session_id, workspace_id=workspace_id
    ):
        result = SpotterAvailability(
            available=False,
            reason=UNAVAILABLE_BLUEPRINT_NOT_INSTALLED,
            message=_NOT_INSTALLED_MESSAGE,
            remedy=f"install the {blueprint_id} Agent Blueprint from the marketplace",
            blueprint_id=blueprint_id,
        )
    else:
        # No session and no workspace: the deployment-level question (the
        # session-defaults picker), where a workspace is supplied later by
        # every real arming.
        refusal = validate_watcher_arming(
            app,
            session_id=session_id,
            workspace_id=workspace_id,
            require_workspace=bool(session_id or workspace_id),
        )
        if refusal is None:
            failed = _watcher_mount_failure(
                app, blueprint_id, session_id=session_id, workspace_id=workspace_id
            )
            if failed is None:
                # Declarations resolve; the server is not started from a GET,
                # so say how far this answer was checked.
                return SpotterAvailability(
                    available=True, blueprint_id=blueprint_id, details={"verified": "static"}
                )
            result = SpotterAvailability(
                available=False,
                reason=UNAVAILABLE_WATCHER_MCP_START_FAILED,
                message=(
                    f"SPOTTER's watcher MCP server {failed.namespace!r} failed to start "
                    f"on its last use: {failed.reason}"
                ),
                remedy=(
                    "fix the cause above, then start a new SPOTTER session "
                    "(a successful start clears this)"
                ),
                blueprint_id=blueprint_id,
                details={
                    "verified": "runtime",
                    "mcp_server": failed.namespace,
                    "mount_reason": failed.reason,
                },
            )
        else:
            result = SpotterAvailability(
                available=False,
                reason=refusal.reason,
                message=refusal.message,
                remedy=refusal.remedy,
                blueprint_id=blueprint_id,
                details=refusal.details(session_id),
            )
    logger.info(
        "spotter_unavailable reason=%s session=%s workspace=%s",
        result.reason,
        session_id,
        workspace_id,
    )
    trace.event(
        "SPOTTER",
        "spotter_unavailable reason=%s session=%s workspace=%s",
        result.reason,
        session_id,
        workspace_id,
    )
    return result


def register_spotter_routes(app: "FastAPI") -> None:
    """Register ``GET /v1/spotter/availability``."""

    import asyncio  # noqa: PLC0415

    @app.get("/v1/spotter/availability")
    async def get_spotter_availability(
        session_id: str = "", workspace_id: str = ""
    ) -> dict[str, Any]:
        # Blueprint discovery and the handoff projection touch the filesystem.
        availability = await asyncio.to_thread(
            spotter_availability, app, session_id=session_id, workspace_id=workspace_id
        )
        return availability.to_wire()


__all__ = [
    "UNAVAILABLE_BLUEPRINT_NOT_INSTALLED",
    "UNAVAILABLE_WATCHER_MCP_START_FAILED",
    "SpotterAvailability",
    "register_spotter_routes",
    "spotter_availability",
]
