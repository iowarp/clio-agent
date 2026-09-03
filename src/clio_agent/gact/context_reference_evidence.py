"""Bounded Evidence-board snapshots behind ``diff`` / ``plan`` / frame references.

Split out of :mod:`clio_agent.gact.context_reference_search` (no-accretion): the
search module owns the cross-repository result shape, this one owns the single
most expensive producer behind it -- a fold over every message part of every
session in a workspace -- plus the bounded snapshot each identity delivers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from clio_agent import conf
from clio_agent.gact.context_reference_result import reference_result as _reference_result

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = [
    "evidence_reference_snapshots",
    "find_evidence_reference_snapshot",
    "snapshot_payload_children",
    "snapshot_payload_string_chars",
]


def _workspace_sessions(app: "FastAPI", workspace_id: str) -> list[Any]:
    """Return the authoritative sessions whose evidence belongs to one workspace."""

    return list(app.state.sessions.list(workspace_id=workspace_id))


def _snapshot_revision(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def snapshot_payload_string_chars() -> int:
    """Character ceiling for one string inside a bounded evidence snapshot.

    Config: ``gact.context_references.snapshot_string_chars`` /
    ``CLIO_CONTEXT_REFERENCE_SNAPSHOT_STRING_CHARS``.
    """

    return conf.resolve(
        "gact.context_references.snapshot_string_chars",
        env="CLIO_CONTEXT_REFERENCE_SNAPSHOT_STRING_CHARS",
        default=4_000,
        cast=conf.as_int,
    )


def snapshot_payload_children() -> int:
    """How many mapping/list children one bounded evidence snapshot level keeps.

    Config: ``gact.context_references.snapshot_children`` /
    ``CLIO_CONTEXT_REFERENCE_SNAPSHOT_CHILDREN``.
    """

    return conf.resolve(
        "gact.context_references.snapshot_children",
        env="CLIO_CONTEXT_REFERENCE_SNAPSHOT_CHILDREN",
        default=50,
        cast=conf.as_int,
    )


def _bounded_payload(
    value: Any,
    *,
    depth: int = 0,
    max_chars: int | None = None,
    max_children: int | None = None,
) -> Any:
    """Keep reference previews useful without copying an unbounded evidence ledger."""

    if max_chars is None:
        # Resolved ONCE at the top of the walk, like the A2UI validator's own
        # string ceiling: the recursion visits every node and is no place for a
        # config lookup.
        max_chars = snapshot_payload_string_chars()
    if max_children is None:
        max_children = snapshot_payload_children()
    if depth >= 6:
        return "[nested content omitted]"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_payload(
                child, depth=depth + 1, max_chars=max_chars, max_children=max_children
            )
            for key, child in list(value.items())[:max_children]
        }
    if isinstance(value, list):
        return [
            _bounded_payload(child, depth=depth + 1, max_chars=max_chars, max_children=max_children)
            for child in value[:max_children]
        ]
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[: max_chars - 1] + "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:max_chars]


def _snapshot_result(
    *,
    kind: str,
    ref_id: str,
    label: str,
    detail: str,
    media_type: str,
    navigation: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    bounded = _bounded_payload(payload)
    assert isinstance(bounded, dict)
    revision = _snapshot_revision(bounded)
    return (
        _reference_result(
            kind=kind,
            ref_id=ref_id,
            label=label,
            detail=detail,
            media_type=media_type,
            revision=revision,
            navigation=navigation,
        ),
        bounded,
    )


def _plan_snapshots(
    app: "FastAPI", workspace_id: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    snapshots: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for session in _workspace_sessions(app, workspace_id):
        for message in app.state.messages.get(session.id, []) or []:
            for index, part in enumerate(message.parts):
                if part.type not in {"plan", "compaction"}:
                    continue
                part_id = part.id or str(index)
                ref_id = f"{session.id}:{message.id}:{part_id}"
                title = part.title or ("Compacted context" if part.type == "compaction" else "Plan")
                payload = {
                    "session_id": session.id,
                    "message_id": message.id,
                    "part_id": part_id,
                    "title": title,
                    "detail": part.summary or part.text,
                    "type": part.type,
                }
                snapshots.append(
                    _snapshot_result(
                        kind="plan",
                        ref_id=ref_id,
                        label=title,
                        detail=f"Plan from {session.title or session.id}",
                        media_type="application/vnd.clio.plan+json",
                        navigation={
                            "workspace_id": workspace_id,
                            "session_id": session.id,
                            "message_id": message.id,
                            "part_id": part_id,
                        },
                        payload=payload,
                    )
                )
    return snapshots


def _diff_snapshots(
    app: "FastAPI", workspace_id: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    snapshots: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rows_by_session = getattr(app.state, "pending_diffs", {})
    for session in _workspace_sessions(app, workspace_id):
        for index, row in enumerate(rows_by_session.get(session.id, []) or []):
            if not isinstance(row, Mapping):
                continue
            path = str(row.get("path") or "Changed file")
            part_id = str(row.get("part_id") or index)
            message_id = str(row.get("message_id") or "")
            ref_id = f"{session.id}:{message_id}:{part_id}"
            payload = {
                "session_id": session.id,
                "message_id": message_id,
                "part_id": part_id,
                "path": path,
                "status": str(row.get("status") or "pending"),
                "unified_diff": str(row.get("unified_diff") or ""),
            }
            snapshots.append(
                _snapshot_result(
                    kind="diff",
                    ref_id=ref_id,
                    label=Path(path).name or path,
                    detail=f"Changed file · {path} · {payload['status']}",
                    media_type="text/x-diff",
                    navigation={
                        "workspace_id": workspace_id,
                        "session_id": session.id,
                        "message_id": message_id,
                        "part_id": part_id,
                        "path": path,
                    },
                    payload=payload,
                )
            )
    return snapshots


def _context_frame_snapshots(
    app: "FastAPI", workspace_id: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    snapshots: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rows_by_session = getattr(app.state, "context_frames", {})
    for session in _workspace_sessions(app, workspace_id):
        for row in rows_by_session.get(session.id, []) or []:
            if not isinstance(row, Mapping) or not row.get("id"):
                continue
            frame_id = str(row["id"])
            item_count = len(row.get("items") or []) if isinstance(row.get("items"), list) else 0
            snapshots.append(
                _snapshot_result(
                    kind="context_frame",
                    ref_id=frame_id,
                    label=f"Context for {session.title or session.id}",
                    detail=f"{row.get('status') or 'assembled'} · {item_count} items",
                    media_type="application/vnd.clio.context-frame+json",
                    navigation={
                        "workspace_id": workspace_id,
                        "session_id": session.id,
                        "frame_id": frame_id,
                        "route": "/v1/sessions/{session_id}/context/frames/{frame_id}",
                    },
                    payload=dict(row),
                )
            )
    return snapshots


def _walk_source_values(
    value: Any, path: tuple[str, ...] = (), depth: int = 0
) -> list[tuple[str, str]]:
    if depth > 5:
        return []
    if isinstance(value, Mapping):
        found: list[tuple[str, str]] = []
        for key, child in list(value.items())[:100]:
            key_text = str(key)
            child_path = (*path, key_text)
            if isinstance(child, str) and _is_referenceable_source(child_path, child):
                found.append((".".join(child_path), child))
            else:
                found.extend(_walk_source_values(child, child_path, depth + 1))
        return found
    if isinstance(value, list):
        found = []
        for index, child in enumerate(value[:100]):
            found.extend(_walk_source_values(child, (*path, str(index)), depth + 1))
        return found
    return []


def _is_web_url(value: str) -> bool:
    """Return whether a metadata value is an externally navigable web source."""

    parsed = urlsplit(value.strip())
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


def _is_referenceable_source(path: tuple[str, ...], value: str) -> bool:
    """Exclude delivery infrastructure URLs from user-facing evidence sources."""

    field = path[-1].replace("-", "_").casefold() if path else ""
    if field in {"endpoint", "processor", "processor_url", "service_endpoint", "service_url"}:
        return False
    return _is_web_url(value)


def _url_source_label(value: str) -> str:
    """Derive a compact human-readable label from a source URL."""

    parsed = urlsplit(value.strip())
    leaf = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]).strip()
    if leaf:
        return f"{leaf} ({parsed.netloc})"
    return parsed.netloc


def _metadata_source_label(path: str, value: str) -> str:
    """Prefer a meaningful metadata field name, falling back to the URL identity."""

    leaf = path.rsplit(".", 1)[-1]
    words = [word for word in leaf.replace("-", "_").split("_") if word]
    if words and words[-1].casefold() == "url":
        words.pop()
    if words and [word.casefold() for word in words] not in (["source"], ["link"]):
        return " ".join(word.capitalize() for word in words)
    return _url_source_label(value)


def _evidence_source_snapshots(
    app: "FastAPI", workspace_id: str
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    snapshots: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for session in _workspace_sessions(app, workspace_id):
        for message in app.state.messages.get(session.id, []) or []:
            for index, part in enumerate(message.parts):
                candidates: list[tuple[str, str, str]] = []
                if (
                    part.type == "resource_link"
                    and part.uri
                    and not part.uri.startswith("artifact://")
                ):
                    candidates.append((part.id or str(index), part.name or part.uri, part.uri))
                for path, value in _walk_source_values(part.metadata):
                    candidates.append(
                        (
                            f"{part.id or index}:{path}",
                            _metadata_source_label(path, value),
                            value,
                        )
                    )
                if part.content_blocks:
                    for path, value in _walk_source_values(part.content_blocks):
                        candidates.append(
                            (
                                f"{part.id or index}:content:{path}",
                                _metadata_source_label(path, value),
                                value,
                            )
                        )
                for source_id, label, value in candidates:
                    dedupe = (session.id, value)
                    if dedupe in seen:
                        continue
                    seen.add(dedupe)
                    digest = hashlib.sha256(
                        f"{session.id}:{message.id}:{source_id}:{value}".encode()
                    ).hexdigest()[:20]
                    ref_id = f"source:{digest}"
                    payload = {
                        "session_id": session.id,
                        "message_id": message.id,
                        "source_id": source_id,
                        "label": label,
                        "value": value,
                    }
                    snapshots.append(
                        _snapshot_result(
                            kind="evidence_source",
                            ref_id=ref_id,
                            label=label,
                            detail=f"From {session.title or session.id}",
                            media_type="text/uri-list"
                            if value.startswith(("http://", "https://"))
                            else "text/plain",
                            navigation={
                                "workspace_id": workspace_id,
                                "session_id": session.id,
                                "message_id": message.id,
                                "uri": value,
                            },
                            payload=payload,
                        )
                    )
    return snapshots


def evidence_reference_snapshots(
    app: "FastAPI", workspace_id: str, kinds: Iterable[str]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return immutable Evidence-board records with their bounded delivery snapshots."""

    selected = set(kinds)
    snapshots: list[tuple[dict[str, Any], dict[str, Any]]] = []
    if "evidence_source" in selected:
        snapshots.extend(_evidence_source_snapshots(app, workspace_id))
    if "context_frame" in selected:
        snapshots.extend(_context_frame_snapshots(app, workspace_id))
    if "diff" in selected:
        snapshots.extend(_diff_snapshots(app, workspace_id))
    if "plan" in selected:
        snapshots.extend(_plan_snapshots(app, workspace_id))
    return snapshots


def find_evidence_reference_snapshot(
    app: "FastAPI", workspace_id: str, kind: str, ref_id: str
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Resolve one Evidence identity from the same inventory used by search."""

    return next(
        (
            snapshot
            for snapshot in evidence_reference_snapshots(app, workspace_id, [kind])
            if snapshot[0]["id"] == ref_id
        ),
        None,
    )
