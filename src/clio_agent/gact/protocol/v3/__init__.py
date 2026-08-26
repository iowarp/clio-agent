"""Public GACT 0.3 projection API."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request

GACT_V3 = "0.3"
A2UI_V091 = "0.9.1"
A2UI_V091_WIRE = "v0.9.1"
CLIO_A2UI_CATALOG_ID = "https://iowarp.ai/a2ui/catalogs/clio-workspace/v1"
CONNECTION_ID = "local"


def utcnow_iso() -> str:
    """Return an ISO-8601 UTC timestamp for protocol provenance."""

    return datetime.now(timezone.utc).isoformat()


def requests_gact_v3(request: Request) -> bool:
    """Return whether a request explicitly negotiated GACT 0.3."""

    return request.headers.get("x-gact-version", "").strip() == GACT_V3


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
    "capabilities_to_v3",
    "event_to_v3",
    "format_sse_v3",
    "message_to_v3",
    "part_to_v3_block",
    "requests_gact_v3",
    "session_to_v3",
    "transcript_entities",
    "utcnow_iso",
    "workspace_to_v3",
]
