"""Cross-repository discovery for structured workspace references."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.wire import mime_for
from clio_agent.gact.routes.workspace_file_policy import (
    is_internal_workspace_file_directory,
    skip_workspace_file_directory,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

_EMPTY_QUERY_LIMIT_PER_KIND = 20
_SEARCH_LIMIT = 100


def _reference_result(
    *,
    kind: str,
    ref_id: str,
    label: str,
    detail: str,
    media_type: str,
    revision: str,
    navigation: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the exact stable result shape shared by every repository."""

    return {
        "kind": kind,
        "id": ref_id,
        "label": label,
        "detail": detail,
        "media_type": media_type,
        "revision": revision,
        "navigation": dict(navigation),
    }


def _disambiguate_duplicate_labels(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Make duplicate labels visibly distinguishable without changing their identities."""

    counts: dict[tuple[str, str], int] = {}
    for row in results:
        key = (str(row["kind"]), str(row["label"]).casefold())
        counts[key] = counts.get(key, 0) + 1
    disambiguated: list[dict[str, Any]] = []
    for row in results:
        key = (str(row["kind"]), str(row["label"]).casefold())
        if counts[key] <= 1 or str(row["id"]) in str(row["detail"]):
            disambiguated.append(row)
            continue
        disambiguated.append({**row, "detail": f"{row['detail']} · {row['id']}"})
    return disambiguated


def _walk_workspace_files(
    app: "FastAPI",
    workspace_id: str,
    *,
    needle: str = "",
    limit: int = _SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    from clio_agent.gact.context_references import (  # noqa: PLC0415
        _file_media_type,
        _stat_revision,
        _workspace_root,
    )

    root = _workspace_root(app, workspace_id)
    if not root.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(
            name
            for name in names
            if not skip_workspace_file_directory(name)
            and not is_internal_workspace_file_directory(name)
        )
        for name in sorted(filenames):
            if len(results) >= limit:
                return results
            path = Path(directory) / name
            if path.is_symlink():
                continue
            try:
                ref_id = path.relative_to(root).as_posix()
                if needle and needle not in f"workspace_file {name} {ref_id}".casefold():
                    continue
                revision = _stat_revision(path)
                size = path.stat().st_size
            except OSError:
                continue
            results.append(
                _reference_result(
                    kind="workspace_file",
                    ref_id=ref_id,
                    label=name,
                    detail=f"{ref_id} ({size} bytes)",
                    media_type=_file_media_type(path),
                    revision=revision,
                    navigation={
                        "workspace_id": workspace_id,
                        "path": ref_id,
                        "route": "/v1/workspaces/{workspace_id}/files/read",
                    },
                )
            )
    return results


def _resource_results(app: "FastAPI", workspace_id: str) -> list[dict[str, Any]]:
    """Project the optional composer resource repository without recreating it."""

    store = getattr(app.state, "resource_store", None)
    if store is None:
        return []
    try:
        records = store.list(workspace_id)
    except (AttributeError, KeyError, TypeError, ValueError):
        return []
    results: list[dict[str, Any]] = []
    for record in records:
        ref_id = str(getattr(record, "id", "") or "")
        label = str(getattr(record, "name", "") or ref_id)
        revision = str(getattr(record, "revision", "") or "")
        media_type = str(
            getattr(record, "detected_mime", "")
            or getattr(record, "media_type", "")
            or "application/octet-stream"
        )
        results.append(
            _reference_result(
                kind="resource",
                ref_id=ref_id,
                label=label,
                detail=f"Uploaded source {label}",
                media_type=media_type,
                revision=revision,
                navigation={
                    "workspace_id": workspace_id,
                    "resource_id": ref_id,
                    "route": "/v1/workspaces/{workspace_id}/resources/{resource_id}",
                },
            )
        )
    return results


def _artifact_results(app: "FastAPI", workspace_id: str) -> list[dict[str, Any]]:
    registry = get_registry(app)
    results: list[dict[str, Any]] = []
    for record in registry.list_for_workspace(workspace_id):
        version = record.head
        if version is None:
            continue
        results.append(
            _reference_result(
                kind="artifact",
                ref_id=version.artifact_id,
                label=record.name,
                detail=f"{record.name} v{version.version} ({version.kind.value})",
                media_type=mime_for(version, record.name),
                revision=f"v{version.version}",
                navigation={
                    "workspace_id": workspace_id,
                    "artifact_id": version.artifact_id,
                    "route": "/v1/artifacts/{artifact_id}",
                },
            )
        )
    return results


def _session_results(app: "FastAPI", workspace_id: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for session in app.state.sessions.list(workspace_id=workspace_id):
        results.append(
            _reference_result(
                kind="session",
                ref_id=session.id,
                label=session.title or session.id,
                detail=f"Session · {session.status} · {session.message_count} messages",
                media_type="application/vnd.clio.session-summary+json",
                revision=session.updated_at,
                navigation={
                    "workspace_id": workspace_id,
                    "session_id": session.id,
                    "route": "/v1/sessions/{session_id}",
                },
            )
        )
    return results


def _agent_run_results(app: "FastAPI", workspace_id: str) -> list[dict[str, Any]]:
    registry = getattr(app.state, "agent_task_registry", None)
    if registry is None:
        return []
    results: list[dict[str, Any]] = []
    for task in registry.snapshot():
        child = app.state.sessions.get(task.child_session_id)
        parent = app.state.sessions.get(task.parent_session_id)
        owner = child or parent
        if owner is None or owner.workspace_id != workspace_id:
            continue
        expert = str((task.agent_ref or {}).get("expert_id") or "agent")
        label = task.run_label or f"{expert} #{task.run_index + 1}"
        results.append(
            _reference_result(
                kind="agent_run",
                ref_id=task.task_id,
                label=label,
                detail=f"Agent run · {task.status} · {expert}",
                media_type="application/vnd.clio.agent-run-summary+json",
                revision=task.updated_at,
                navigation={
                    "workspace_id": workspace_id,
                    "task_id": task.task_id,
                    "session_id": task.child_session_id,
                    "route": "/v1/agent-tasks/{task_id}",
                },
            )
        )
    return results


def _workspace_sessions(app: "FastAPI", workspace_id: str) -> list[Any]:
    """Return the authoritative sessions whose evidence belongs to one workspace."""

    return list(app.state.sessions.list(workspace_id=workspace_id))


def _snapshot_revision(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _bounded_payload(value: Any, *, depth: int = 0) -> Any:
    """Keep reference previews useful without copying an unbounded evidence ledger."""

    if depth >= 6:
        return "[nested content omitted]"
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_payload(child, depth=depth + 1)
            for key, child in list(value.items())[:50]
        }
    if isinstance(value, list):
        return [_bounded_payload(child, depth=depth + 1) for child in value[:50]]
    if isinstance(value, str):
        return value if len(value) <= 4_000 else value[:3_999] + "…"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:4_000]


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
            if (
                isinstance(child, str)
                and child
                and (
                    child.startswith(("http://", "https://"))
                    or key_text.casefold().endswith(
                        ("source", "source_url", "provenance", "provenance_url")
                    )
                )
            ):
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
                        (f"{part.id or index}:{path}", path.rsplit(".", 1)[-1], value)
                    )
                if part.content_blocks:
                    for path, value in _walk_source_values(part.content_blocks):
                        candidates.append(
                            (f"{part.id or index}:content:{path}", "MCP source", value)
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
                            detail=f"Source from {session.title or session.id} · {value}",
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


async def search_workspace_references(
    app: "FastAPI",
    workspace_id: str,
    *,
    query: str = "",
    kinds: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Search existing same-workspace repositories using one stable result shape."""

    from clio_agent.gact.context_references import (  # noqa: PLC0415
        REFERENCE_SEARCH_KINDS,
        _failure,
    )

    if app.state.workspaces.get(workspace_id) is None:
        raise _failure(
            status_code=404,
            error="not_found",
            message=f"workspace not found: {workspace_id}",
            ref_kind="workspace",
            ref_id=workspace_id,
        )
    selected = set(kinds or REFERENCE_SEARCH_KINDS)
    unknown = sorted(selected - REFERENCE_SEARCH_KINDS)
    if unknown:
        raise _failure(
            status_code=400,
            error="validation_error",
            message=f"unknown reference kind(s): {', '.join(unknown)}",
            ref_kind="reference_search",
            ref_id=workspace_id,
            recoverable=True,
            unknown_kinds=unknown,
            allowed_kinds=sorted(REFERENCE_SEARCH_KINDS),
        )

    needle = query.strip().casefold()
    results: list[dict[str, Any]] = []
    if "workspace_file" in selected:
        results.extend(
            await asyncio.to_thread(
                _walk_workspace_files,
                app,
                workspace_id,
                needle=needle,
                limit=_SEARCH_LIMIT if needle else _EMPTY_QUERY_LIMIT_PER_KIND,
            )
        )
    if "resource" in selected:
        results.extend(_resource_results(app, workspace_id))
    if "artifact" in selected:
        results.extend(await asyncio.to_thread(_artifact_results, app, workspace_id))
    if "session" in selected:
        results.extend(_session_results(app, workspace_id))
    if "agent_run" in selected:
        results.extend(_agent_run_results(app, workspace_id))
    evidence_kinds = selected & {"evidence_source", "context_frame", "diff", "plan"}
    if evidence_kinds:
        results.extend(
            row for row, _payload in evidence_reference_snapshots(app, workspace_id, evidence_kinds)
        )

    results = _disambiguate_duplicate_labels(results)
    if needle:
        results = [
            row
            for row in results
            if needle
            in " ".join(
                (str(row["label"]), str(row["detail"]), str(row["id"]), str(row["kind"]))
            ).casefold()
        ]
    ordered = sorted(results, key=lambda row: (row["kind"], row["label"].casefold(), row["id"]))
    if needle:
        return ordered[:_SEARCH_LIMIT]

    counts: dict[str, int] = {}
    bounded: list[dict[str, Any]] = []
    for row in ordered:
        kind = str(row["kind"])
        count = counts.get(kind, 0)
        if count >= _EMPTY_QUERY_LIMIT_PER_KIND:
            continue
        counts[kind] = count + 1
        bounded.append(row)
    return bounded


__all__ = [
    "evidence_reference_snapshots",
    "find_evidence_reference_snapshot",
    "search_workspace_references",
]
