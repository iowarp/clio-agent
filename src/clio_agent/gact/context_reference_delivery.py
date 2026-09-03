"""Model-facing delivery of admitted ``context_ref`` parts.

The admission half (:mod:`clio_agent.gact.context_references`) decides whether a
client-supplied handle may become a durable message part. THIS module is the
read side: it turns the already-admitted parts into the bounded blocks the model
sees, into the delivery records the transcript carries, and into the
provenance-bearing context-frame rows.

Split out of ``context_references`` so neither half becomes a god-file (the
admission module was one edit from the 800-line cap) and so the direction of
dependence is one-way: delivery reads the resolvers, never the reverse.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.context_reference_domain import ContextReferenceError
from clio_agent.gact.context_reference_file_io import read_bounded_file as _read_bounded_file
from clio_agent.gact.context_references import (
    SUMMARY_REFERENCE_KINDS,
    _artifact_path,
    _contained_file,
    _failure,
    _resolve_part_sync,
)
from clio_agent.gact.runtime.constants import _CTX_MAX_BYTES
from clio_agent.gact.runtime.globals import _ContextFileAccessError
from clio_agent.gact.types import ErrorInfo, Message, Part

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = [
    "context_reference_deliveries",
    "context_reference_frame_items",
    "enrich_with_context_references",
    "record_context_reference_deliveries",
]


def context_reference_deliveries(parts: Iterable[Part]) -> list[dict[str, Any]]:
    """Copy server-resolved delivery records from canonical message parts."""

    deliveries: list[dict[str, Any]] = []
    for part in parts:
        if part.type != "context_ref":
            continue
        metadata = part.metadata.get("context_reference")
        if isinstance(metadata, Mapping):
            deliveries.append(copy.deepcopy(dict(metadata)))
    return deliveries


def record_context_reference_deliveries(metadata: dict[str, Any], parts: Iterable[Part]) -> None:
    """Attach canonical context-reference delivery records to message metadata."""

    deliveries = context_reference_deliveries(parts)
    if deliveries:
        metadata["context_reference_deliveries"] = deliveries


def _recorded_delivery_metadata(part: Part, workspace_id: str) -> tuple[dict[str, Any], bool]:
    """Return the snapshot recorded at admission, after rechecking ownership.

    Identity and ownership are still verified (kind, ref_id, workspace, and the
    revision the part itself pins); only CURRENCY is not required. That is the
    whole point: the four evidence kinds are snapshots of state that moves inside
    the very turn that referenced them, so an admitted message must stay
    deliverable after the state moved.
    """

    recorded = part.metadata.get("context_reference")
    if not isinstance(recorded, Mapping):
        return {}, False
    if (
        recorded.get("kind") != part.ref_kind
        or recorded.get("ref_id") != part.ref_id
        or recorded.get("workspace_id") != workspace_id
        or recorded.get("resolved_revision") != part.revision
    ):
        return {}, False
    return copy.deepcopy(dict(recorded)), True


def _summary_block(part: Part, metadata: Mapping[str, Any], *, stale: bool) -> str:
    """Render one bounded summary block, stamped with the revision it is as-of."""

    summary = metadata["delivery"]["summary"]
    marker = " (as-of snapshot; current state has moved)" if stale else ""
    return (
        f"### {part.ref_kind}: {part.label} "
        f"[{part.ref_id}@{metadata.get('resolved_revision', part.revision)}]{marker}\n"
        f"{json.dumps(summary, sort_keys=True)}"
    )


def _summary_delivery(app: "FastAPI", workspace_id: str, part: Part) -> tuple[dict[str, Any], bool]:
    """Resolve one summary-kind reference, preferring its recorded snapshot.

    The recorded snapshot is consulted BEFORE revision enforcement, not after.
    Enforcing first made the recorded-delivery resilience path unreachable for the
    four evidence kinds: a context_frame or diff goes stale within its own turn,
    so a 409 at delivery time made an ALREADY ADMITTED message permanently
    un-retryable -- the row was durable and every retry hit the same wall. The
    admitted snapshot is the honest answer to "what did the user attach?", and the
    ``stale`` marker below tells the model (and the trace) that current state has
    moved on.
    """

    recorded, uses_recorded = _recorded_delivery_metadata(part, workspace_id)
    if uses_recorded:
        try:
            _canonical, current = _resolve_part_sync(app, workspace_id, part, enforce_revision=True)
        except ContextReferenceError as exc:
            if exc.error != "context_ref_stale":
                # Gone / inaccessible / no longer ours is a REAL refusal: the
                # snapshot is only a stand-in for state that moved, never for
                # state the caller may no longer see.
                raise
            recorded["stale"] = {
                "reason": "context_ref_stale",
                "recorded_revision": str(part.revision),
                "actual_revision": str(exc.details.get("actual_revision") or ""),
            }
            return recorded, True
        return current, False
    _canonical, current = _resolve_part_sync(app, workspace_id, part, enforce_revision=True)
    return current, False


def _inline_content_path(app: "FastAPI", workspace_id: str, part: Part) -> Path | None:
    if part.ref_kind == "workspace_file":
        _root, path = _contained_file(app, workspace_id, part.ref_id)
        return path
    found = get_registry(app).get_by_artifact_id(part.ref_id)
    if found is None:  # pragma: no cover - the resolver already refused this
        return None
    return _artifact_path(app, found[0], found[1])


def _inline_block(
    app: "FastAPI", workspace_id: str, part: Part, metadata: Mapping[str, Any]
) -> str:
    """Render one inline-text block after re-checking the bytes still match."""

    content_path = _inline_content_path(app, workspace_id, part)
    if content_path is None:
        return _metadata_block(part, metadata)
    snapshot = _read_bounded_file(content_path)
    delivery = metadata["delivery"]
    expected_sha = str(delivery.get("sha256") or "")
    if expected_sha and snapshot.sha256 != expected_sha:
        raise _failure(
            status_code=409,
            error="context_ref_stale",
            message="context reference changed while preparing delivery",
            ref_kind=part.ref_kind,
            ref_id=part.ref_id,
            recoverable=True,
            expected_sha256=expected_sha,
            actual_sha256=snapshot.sha256,
        )
    text = snapshot.data.decode("utf-8", errors="replace")
    truncated = snapshot.size_bytes - len(snapshot.data)
    suffix = f"\n... ({truncated} more bytes truncated)" if truncated > 0 else ""
    return (
        f"### {part.ref_kind}: {part.label} "
        f"[{part.ref_id}@{part.revision}]\n```\n{text}{suffix}\n```"
    )


def _metadata_block(part: Part, metadata: Mapping[str, Any]) -> str:
    delivery = metadata["delivery"]
    return (
        f"### {part.ref_kind}: {part.label} "
        f"[{part.ref_id}@{part.revision}]\n"
        f"media_type={metadata['media_type']}; delivery=metadata-only; "
        f"sha256={delivery.get('sha256', '')}"
    )


def _reference_block(app: "FastAPI", workspace_id: str, part: Part) -> str:
    if part.ref_kind in SUMMARY_REFERENCE_KINDS:
        metadata, stale = _summary_delivery(app, workspace_id, part)
        return _summary_block(part, metadata, stale=stale)
    canonical, metadata = _resolve_part_sync(app, workspace_id, part, enforce_revision=True)
    if metadata["delivery"]["mode"] == "inline_text":
        return _inline_block(app, workspace_id, canonical, metadata)
    return _metadata_block(canonical, metadata)


def enrich_with_context_references(
    app: "FastAPI", sid: str, user_text: str, user_message: Message
) -> str:
    """Resolve typed parts again and prepend their bounded model-facing content."""

    session = app.state.sessions.get(sid)
    if session is None:
        return user_text
    blocks: list[str] = []
    try:
        for part in user_message.parts:
            if part.type != "context_ref":
                continue
            blocks.append(_reference_block(app, session.workspace_id, part))
    except (ContextReferenceError, OSError) as exc:
        raise _ContextFileAccessError(_delivery_error_info(exc)) from exc
    if not blocks:
        return user_text
    return (
        "## Structured context references (server-resolved)\n\n"
        + "\n\n".join(blocks)
        + "\n\n## User question\n\n"
        + user_text
    )


def _delivery_error_info(exc: ContextReferenceError | OSError) -> ErrorInfo:
    if isinstance(exc, ContextReferenceError):
        return ErrorInfo(
            error=exc.error,
            message=exc.message,
            details=exc.details,
            recoverable=exc.recoverable,
        )
    return ErrorInfo(
        error="context_ref_inaccessible",
        message="context reference could not be read for delivery",
        details={"operation": "read", "original_error": type(exc).__name__},
        recoverable=True,
    )


def context_reference_frame_items(message: Message) -> list[dict[str, Any]]:
    """Return provenance-bearing frame rows for resolved context-reference parts."""

    rows: list[dict[str, Any]] = []
    for part in message.parts:
        if part.type != "context_ref":
            continue
        resolved = part.metadata.get("context_reference", {})
        delivery = resolved.get("delivery", {})
        size_bytes = delivery.get("size_bytes", 0)
        if isinstance(size_bytes, int) and size_bytes > 0:
            tokens = min(size_bytes, _CTX_MAX_BYTES) // 4
        else:
            tokens = len(json.dumps(delivery.get("summary", {}), sort_keys=True)) // 4
        rows.append(
            {
                "kind": "context_ref",
                "source_id": part.ref_id,
                "included": True,
                "reason": "structured_context_reference",
                "tokens_estimated": tokens,
                "metadata": {
                    "ref_kind": part.ref_kind,
                    "label": part.label,
                    "revision": part.revision,
                    "workspace_id": resolved.get("workspace_id", ""),
                    "delivery": resolved.get("delivery", {}),
                    "provenance": resolved.get("provenance", {}),
                },
            }
        )
    return rows
