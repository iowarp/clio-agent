"""Display retained NDP results whose original adapters emitted empty output blocks."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from clio_agent.gact.storage.ndp_presentations import endpoint_presentation, ndp_presentation

_DISCOVERY = {"connected_data_search_ndp", "connected_data_ndp_organizations"}
_ENDPOINT = {
    "connected_data_ndp_endpoint_connect",
    "connected_data_ndp_endpoint_operations",
    "connected_data_ndp_endpoint_request",
    "connected_data_ndp_upload_artifact",
}


def legacy_ndp_presentation(
    part: Mapping[str, Any],
    args: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Recover display from recorded output only; never rerun a request or edit its ledger."""
    name = part.get("tool_name")
    if (
        part.get("type") != "tool_result"
        or part.get("is_error")
        or name not in _DISCOVERY | _ENDPOINT
    ):
        return None
    presentation = part.get("presentation")
    if not isinstance(presentation, Mapping) or presentation.get("blocks"):
        return None
    metadata = part.get("metadata")
    result = metadata.get("result") if isinstance(metadata, Mapping) else None
    if not isinstance(result, Mapping):
        return None
    if result.get("truncated") and isinstance(result.get("preview"), str):
        try:
            result = json.loads(result["preview"])
        except ValueError:
            return {
                **presentation,
                "status": "degraded",
                "summary": "Only a partial result was retained",
                "blocks": [
                    {
                        "id": "recorded-limit",
                        "type": "text",
                        "severity": "warning",
                        "text": "Only a truncated result was retained in this older activity record. Its exact preview remains in technical details.",
                    },
                ],
            }
    return (ndp_presentation if name in _DISCOVERY else endpoint_presentation)(
        args or {}, result, None
    )
