"""Select response deliverables without deleting their immutable evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    from fastapi import FastAPI

PURPOSES = frozenset({"deliverable", "intermediate", "verification"})


def artifact_purpose(producer: dict[str, Any]) -> str:
    """Read explicit presentation intent, preserving legacy output defaults."""
    if producer.get("designation") in {"document-rendition", "a2ui_visual_capture"}:
        return "verification"
    return str(producer.get("purpose") or "deliverable")


def child_response_owners(app: "FastAPI", session_ids: set[str]) -> dict[str, str]:
    """Read explicit child publication, including reused verification artifacts.

    Immutable producers describe origin. A child's persisted response link also
    records its later decision to publish those bytes without rewriting origin.
    """
    messages = getattr(app.state, "messages", {})
    return {
        str(part.metadata["artifact_id"]): sid
        for sid in sorted(session_ids)
        for message in messages.get(sid, [])
        if message.role == "assistant"
        for part in message.parts
        if part.type == "resource_link" and part.metadata.get("artifact_id")
    }


def response_deliverables(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the latest deliverable per artifact family and causal producer.

    This is a response projection only. Versions, files, events and lineage remain
    intact. Distinct child sessions and agents never collapse by filename.
    A per-call purpose overrides a deduplicated version's original intent.
    """
    selected: dict[tuple[str, ...], dict[str, Any]] = {}
    for entry in entries:
        version = entry.get("version")
        if version is None:
            continue
        producer = version.producer or {}
        if producer.get("designation") == "document-rendition":
            continue
        purpose = entry.get("purpose") or artifact_purpose(producer)
        if purpose != "deliverable":
            continue
        sid = str(entry.get("response_session_id") or producer.get("session_id") or "")
        # Unattributed evidence has no authoritative causal family to combine.
        owner = (
            (
                sid,
                str(producer.get("agent_id") or ""),
            )
            if sid
            else (version.artifact_id,)
        )
        key = (str(entry.get("workspace_id") or ""), str(entry.get("name") or ""), *owner)
        previous = selected.get(key)
        if previous is None or version.version >= previous["version"].version:
            selected[key] = entry
    return list(selected.values())
