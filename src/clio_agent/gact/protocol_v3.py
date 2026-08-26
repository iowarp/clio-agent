"""Compatibility facade for the versioned GACT 0.3 projection package."""

from clio_agent.gact.protocol.v3 import (
    A2UI_V091,
    A2UI_V091_WIRE,
    CLIO_A2UI_CATALOG_ID,
    CONNECTION_ID,
    GACT_V3,
    capabilities_to_v3,
    event_to_v3,
    format_sse_v3,
    message_to_v3,
    part_to_v3_block,
    requests_gact_v3,
    session_to_v3,
    transcript_entities,
    utcnow_iso,
    workspace_to_v3,
)

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
