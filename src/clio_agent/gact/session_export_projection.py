"""Use the live transcript projection with complete retained archive results."""

from __future__ import annotations

import mimetypes
from copy import deepcopy
from typing import Any

from clio_agent.gact.agents.recorded_inspection_presentation import legacy_inspection_presentation
from clio_agent.gact.agents.skill_presentation import legacy_skill_presentation
from clio_agent.gact.protocol.v3.message import transcript_entities
from clio_agent.gact.ssh_recorded_presentation import legacy_ssh_presentation
from clio_agent.gact.storage.ndp_recorded_presentation import legacy_ndp_presentation


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
        calls = {
            p["call_id"]: p.get("input")
            for m in row["messages"]
            for p in m.get("parts", [])
            if p.get("type") == "tool_call" and p.get("call_id")
        }
        presentations: dict[str, dict[str, Any]] = {}
        for message in row["messages"]:
            for part in message.get("parts", []):
                call_id = part.get("call_id")
                observed = part
                record = records.get(call_id)
                # A durable trace can retain a complete observation where older
                # message metadata kept only a preview. Use those recorded bytes
                # for display, without rewriting either copy of the evidence.
                if part.get("type") == "tool_result" and record and "output" in record:
                    result = record["output"]
                    if isinstance(result, dict):
                        result = result.get(
                            "structuredContent", result.get("structured_content", result)
                        )
                    observed = {
                        **part,
                        "metadata": {**(part.get("metadata") or {}), "result": result},
                    }
                value = (
                    legacy_ssh_presentation(observed)
                    or legacy_ndp_presentation(observed, calls.get(call_id))
                    or legacy_skill_presentation(observed, calls.get(call_id))
                    or legacy_inspection_presentation(observed, calls.get(call_id))
                    or part.get("presentation")
                )
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
                if "status" not in presentations[tool["id"]]:
                    # A complete retained result supersedes a limitation inferred
                    # from the bounded message preview. Execution state is separate.
                    tool["presentation"].pop("status", None)
        views[sid] = projection
    return views


def with_minted_artifacts(views: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    """Add each session's minted artifacts to its view, as the live conversation has them.

    A view's artifacts come from the transcript's artifact parts, so a file a tool minted
    and an answer shows by its ``artifact://`` link (a browser screenshot) had no record
    offline, and the page said the image was unavailable although the export carried it.
    """
    for entry in manifest.get("artifacts") or ():
        sid = str((entry.get("producer") or {}).get("session_id") or "")
        artifact_id = str(entry.get("artifact_id") or "")
        view = views.get(sid)
        if view is None or not artifact_id:
            continue
        known = view.setdefault("artifacts", [])
        if any(isinstance(item, dict) and item.get("id") == artifact_id for item in known):
            continue
        name = str(entry.get("name") or artifact_id)
        media_type = (
            mimetypes.guess_type(name)[0] or mimetypes.guess_type(str(entry.get("path") or ""))[0]
        )
        size = (entry.get("evidence") or {}).get("size_bytes")
        known.append(
            {
                "id": artifact_id,
                "session_id": sid,
                "workspace_id": str(entry.get("workspace_id") or ""),
                "name": name,
                "media_type": media_type or "application/octet-stream",
                "uri": f"artifact://{artifact_id}",
                **({"version": entry["version"]} if isinstance(entry.get("version"), int) else {}),
                **({"size": size} if isinstance(size, int) else {}),
            }
        )
    return views
