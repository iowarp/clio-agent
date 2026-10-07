"""Use the live transcript projection with complete retained archive results."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from clio_agent.gact.protocol.v3.message import transcript_entities


def archive_transcript_views(transcript: dict[str, Any]) -> dict[str, Any]:
    """Keep live message grouping while embedding durable tool bodies, not previews.

    The original ledger remains unchanged. Calls and results share the same
    invocation identity and location, exactly as in the live transcript.
    Full declared presentation bodies make native expansion work offline.
    """
    views: dict[str, Any] = {}
    for row in [transcript, *transcript.get("children", [])]:
        sid = row["session"]["id"]
        projection = transcript_entities(row["messages"], sid)
        if "interactions" in row:
            projection["interactions"] = row["interactions"]
        records = {record["call_id"]: record for record in row.get("tool_records", [])}
        presentations: dict[str, dict[str, Any]] = {}
        for message in row["messages"]:
            for part in message.get("parts", []):
                call_id = part.get("call_id")
                value = part.get("presentation")
                if not call_id or not value:
                    continue
                previous = presentations.get(call_id, {})
                blocks = {block["id"]: deepcopy(block) for block in previous.get("blocks", [])}
                for block in value.get("blocks", []):
                    blocks[block["id"]] = {**blocks.get(block["id"], {}), **deepcopy(block)}
                presentations[call_id] = {
                    **previous,
                    **deepcopy(value),
                    "blocks": list(blocks.values()),
                }
        for tool in projection["tools"]:
            record = records.get(tool["id"])
            if record:
                tool["input"] = record.get("input", tool.get("input"))
                tool["output"] = record.get("output", tool.get("output"))
                if record.get("tool"):
                    tool["name"] = record["tool"]
                if record.get("error"):
                    tool["error"] = record["error"]
            if tool["id"] in presentations:
                # Preserve adapter-projected headers, replace only bounded bodies.
                tool["presentation"] = {
                    **(tool.get("presentation") or {}),
                    **presentations[tool["id"]],
                }
        views[sid] = projection
    return views
