"""Registered artifact return and parent-context delivery for agent tasks.

An :class:`~clio_agent.gact.agent_tasks.AgentTask` result keeps a bounded answer
excerpt, but a commissioned blueprint can produce a richer registered report.
This module owns the bridge between those two existing substrates: select the
artifact explicitly linked from the child's final message, store its real
``ArtifactRef`` on the task, and resolve verified textual content when the
parent actually collects the task.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.records import ArtifactKind, ArtifactRecord, ArtifactVersion, Custody
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.wire import artifact_uri, fetch_url_for, mime_for

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

ArtifactRefValue = dict[str, Any] | str


def returned_artifact_ref(app: "FastAPI", final_message: Any) -> dict[str, Any]:
    """Return the registered artifact linked by a child's final message.

    A report is preferred when the final message links more than one artifact;
    otherwise the last linked artifact is the child's designated return.  The
    returned object is the canonical ``ArtifactVersion.to_artifact_ref`` shape,
    augmented only with routing metadata needed to retrieve and display it.
    """

    parts = list(getattr(final_message, "parts", None) or [])
    linked_ids = [
        str((getattr(part, "metadata", None) or {}).get("artifact_id") or "")
        for part in parts
        if getattr(part, "type", "") == "resource_link"
        and str(getattr(part, "server_id", "") or "") == "clio-artifacts"
    ]
    if not linked_ids:
        return {}
    registry = get_registry(app)
    candidates: list[tuple[ArtifactRecord, ArtifactVersion]] = []
    for artifact_id in linked_ids:
        found = registry.get_by_artifact_id(artifact_id) if artifact_id else None
        if found is not None:
            candidates.append(found)
    if not candidates:
        return {}
    reports = [row for row in candidates if row[1].kind == ArtifactKind.REPORT]
    record, version = (reports or candidates)[-1]
    ref = dict(version.to_artifact_ref())
    metadata = dict(ref.get("metadata") or {})
    metadata.update(
        {
            "workspace_id": record.workspace_id,
            "name": record.name,
            "uri": artifact_uri(record.workspace_id, record.name, version.version),
            "fetch_url": fetch_url_for(version.artifact_id),
            "media_type": mime_for(version, record.name),
        }
    )
    ref["metadata"] = metadata
    return ref


def artifact_context_for_task(app: "FastAPI", task: Any) -> dict[str, Any]:
    """Resolve a task's returned artifact into verified parent model context.

    The reference is always returned. Textual bytes are hash-verified before
    decoding and included up to the configured context bound; an unavailable,
    binary, corrupt, or oversize result is explicit in the returned metadata.
    """

    raw_ref = getattr(task, "artifact_ref", "")
    if not isinstance(raw_ref, Mapping):
        return {"artifact_ref": raw_ref} if raw_ref else {}
    ref = dict(raw_ref)
    artifact_id = str(ref.get("artifact_id") or "")
    found = get_registry(app).get_by_artifact_id(artifact_id) if artifact_id else None
    if found is None:
        return {"artifact_ref": ref, "content_status": "artifact_unavailable"}
    record, version = found
    context: dict[str, Any] = {
        "artifact_ref": ref,
        "name": record.name,
        "media_type": mime_for(version, record.name),
    }
    if not _is_textual(version, record.name):
        return {**context, "content_status": "non_text_artifact"}
    source = _artifact_source(app, record, version)
    if source is None:
        return {**context, "content_status": "artifact_bytes_unavailable"}
    try:
        payload = source.read_bytes()
    except OSError:
        return {**context, "content_status": "artifact_bytes_unreadable"}
    if version.sha256 and hashlib.sha256(payload).hexdigest() != version.sha256:
        return {**context, "content_status": "integrity_violation"}
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        return {**context, "content_status": "text_decode_failed"}

    cap = _artifact_context_chars()
    if len(content) > cap:
        return {
            **context,
            "content": content[:cap],
            "content_status": "truncated",
            "original_chars": len(content),
            "included_chars": cap,
        }
    return {**context, "content": content, "content_status": "complete"}


def artifact_context_text(context: Mapping[str, Any]) -> str:
    """Serialize one artifact context as a structurally delimited model block."""

    if not context:
        return ""
    content = str(context.get("content") or "")
    safe_content = content.replace("</commissioned-artifact>", "[closing tag removed]")
    metadata = {key: value for key, value in context.items() if key != "content"}
    return (
        "- returned_artifact: "
        + json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
        + "\n<commissioned-artifact>\n"
        + safe_content
        + "\n</commissioned-artifact>"
    )


def record_parent_artifact_use(app: "FastAPI", parent_session_id: str, task: Any) -> bool:
    """Record the first delivery of a commissioned artifact to its parent.

    Returns ``True`` only for the first materialized use edge, which lets the
    caller emit the corresponding commission lifecycle event exactly once.
    """

    raw_ref = getattr(task, "artifact_ref", "")
    if not isinstance(raw_ref, Mapping):
        return False
    artifact_id = str(raw_ref.get("artifact_id") or "")
    found = get_registry(app).get_by_artifact_id(artifact_id) if artifact_id else None
    if found is None:
        return False
    record, version = found
    try:
        from clio_agent.gact.artifacts.registry import ARTIFACT_USED_EVENT  # noqa: PLC0415
        from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
            _active_semantic_trace_id,
            _active_semantic_turn_id,
            _emit_semantic_event,
        )
        from clio_agent.gact.semantic_events import _event_id  # noqa: PLC0415

        event_id = _event_id()
        if not get_registry(app).record_artifact_used(
            parent_session_id, artifact_id, event_id=event_id
        ):
            return False
        subject = {
            "artifact_id": artifact_id,
            "name": record.name,
            "workspace_id": record.workspace_id,
        }
        _emit_semantic_event(
            app,
            parent_session_id,
            ARTIFACT_USED_EVENT,
            turn_id=_active_semantic_turn_id(),
            trace_id=_active_semantic_trace_id(),
            status="completed",
            summary=f"Parent session used commissioned artifact {record.name} v{version.version}.",
            actor={"session_id": parent_session_id, "mechanism": "agent_task_return"},
            subject=subject,
            payload={
                **subject,
                "event_id": event_id,
                "version": version.version,
                "session_id": parent_session_id,
                "task_id": str(getattr(task, "task_id", "") or ""),
                "reason": "commission_return_context",
            },
            detail_level="semantic",
        )
        return True
    except Exception:  # noqa: BLE001 - provenance decoration must not break task collection
        logger.warning(
            "commission artifact use emit skipped reason=artifact_use_emit_failed task=%s",
            getattr(task, "task_id", "?"),
        )
        return False


def emit_commission_parent_use(app: "FastAPI", parent_session_id: str, task: Any) -> bool:
    """Record and expose the first parent-context use of a commission result."""

    if not record_parent_artifact_use(app, parent_session_id, task):
        return False
    blueprint_id = str((getattr(task, "agent_ref", None) or {}).get("blueprint_id") or "")
    if not blueprint_id:
        return True
    from clio_agent.gact.runtime.globals import (  # noqa: PLC0415
        _active_semantic_trace_id,
        _active_semantic_turn_id,
        _emit_semantic_event,
    )

    raw_ref = getattr(task, "artifact_ref", "")
    artifact_id = str(raw_ref.get("artifact_id") or "") if isinstance(raw_ref, Mapping) else ""
    _emit_semantic_event(
        app,
        parent_session_id,
        "blueprint.commission.parent_used_artifact",
        turn_id=_active_semantic_turn_id(),
        trace_id=_active_semantic_trace_id(),
        status="completed",
        summary=f"Parent used the report returned by {blueprint_id}.",
        actor={"session_id": parent_session_id, "role": "commissioning_parent"},
        subject={"artifact_id": artifact_id, "task_id": getattr(task, "task_id", "")},
        blueprint={
            "agent_blueprint_id": blueprint_id,
            "parent_expert": str(
                (getattr(task, "agent_ref", None) or {}).get("requesting_expert_id") or ""
            ),
            "child_expert": str((getattr(task, "agent_ref", None) or {}).get("expert_id") or ""),
        },
        payload={
            "task_id": str(getattr(task, "task_id", "") or ""),
            "artifact_ref": raw_ref,
        },
    )
    return True


def _artifact_context_chars() -> int:
    from clio_agent import conf  # noqa: PLC0415

    return max(
        1,
        conf.resolve(
            "limits.agent_task_artifact_context_chars",
            env="CLIO_AGENT_TASK_ARTIFACT_CONTEXT_CHARS",
            default=64_000,
            cast=conf.as_int,
        ),
    )


def _is_textual(version: ArtifactVersion, name: str) -> bool:
    media_type = mime_for(version, name)
    return media_type.startswith("text/") or media_type in {
        "application/json",
        "application/yaml",
    }


def _artifact_source(
    app: "FastAPI", record: ArtifactRecord, version: ArtifactVersion
) -> Path | None:
    workspace = app.state.workspaces.get(record.workspace_id)
    workspace_root_text = str(getattr(workspace, "root_path", "") or "")
    workspace_root = Path(workspace_root_text).expanduser().resolve(strict=False)
    if version.custody == Custody.CAS and version.sha256 and workspace_root_text:
        from clio_agent.gact.artifacts.cas import CASStore  # noqa: PLC0415

        candidate = CASStore(workspace_root).blob_path(version.sha256)
        if candidate.is_file():
            return candidate
    referenced_candidate = Path(version.path).expanduser() if version.path else None
    return (
        referenced_candidate
        if referenced_candidate is not None and referenced_candidate.is_file()
        else None
    )


__all__ = [
    "ArtifactRefValue",
    "artifact_context_for_task",
    "artifact_context_text",
    "emit_commission_parent_use",
    "record_parent_artifact_use",
    "returned_artifact_ref",
]
