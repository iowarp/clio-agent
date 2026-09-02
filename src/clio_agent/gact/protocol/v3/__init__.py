"""Public GACT 0.3 projection API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import Request

from clio_agent.gact.protocol.constants import (
    A2UI_V091,
    A2UI_V091_WIRE,
    CLIO_A2UI_CATALOG_ID,
    GACT_V2,
    GACT_V3,
)

CONNECTION_ID = "local"


def utcnow_iso() -> str:
    """Return an ISO-8601 UTC timestamp for protocol provenance."""

    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Projection:
    """One projected 0.3 envelope body: its type, payload and entity identity.

    Defined here rather than beside the projector table so a per-lane projector
    module (``protocol.v3.composer``) can produce one without importing back
    into ``protocol.v3.event`` — which registers it — and forming a cycle.
    """

    event_type: str
    payload: dict[str, Any]
    entity_id: str | None = None


def project_for_request(
    request: Request,
    *,
    v3: Callable[[], Any],
    v2: Callable[[], Any],
) -> Any:
    """Select a projection from the version negotiated by middleware."""

    version = str(getattr(request.state, "protocol_version", GACT_V2))
    registry = {GACT_V3: v3, GACT_V2: v2}
    try:
        return registry[version]()
    except KeyError as exc:  # middleware should make this unreachable
        raise RuntimeError(f"No projection is registered for GACT {version}") from exc


from clio_agent.gact.protocol.v3.capabilities import capabilities_to_v3
from clio_agent.gact.protocol.v3.event import event_to_v3, format_sse_v3
from clio_agent.gact.protocol.v3.message import (
    message_to_v3,
    part_to_v3_block,
    transcript_entities,
)
from clio_agent.gact.protocol.v3.session import session_to_v3
from clio_agent.gact.protocol.v3.workspace import workspace_to_v3

__all__ = [
    "A2UI_V091",
    "A2UI_V091_WIRE",
    "CLIO_A2UI_CATALOG_ID",
    "CONNECTION_ID",
    "GACT_V3",
    "GACT_V2",
    "capabilities_to_v3",
    "event_to_v3",
    "format_sse_v3",
    "message_to_v3",
    "part_to_v3_block",
    "project_for_request",
    "session_to_v3",
    "transcript_entities",
    "utcnow_iso",
    "workspace_to_v3",
]
