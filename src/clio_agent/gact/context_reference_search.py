"""Cross-repository discovery for structured workspace references."""

from __future__ import annotations

import asyncio
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

_SEARCH_LIMIT = 5000


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


def _walk_workspace_files(app: "FastAPI", workspace_id: str) -> list[dict[str, Any]]:
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
            if len(results) >= _SEARCH_LIMIT:
                return results
            path = Path(directory) / name
            if path.is_symlink():
                continue
            try:
                ref_id = path.relative_to(root).as_posix()
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
                detail=f"Workspace resource {label}",
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

    results: list[dict[str, Any]] = []
    if "workspace_file" in selected:
        results.extend(await asyncio.to_thread(_walk_workspace_files, app, workspace_id))
    if "resource" in selected:
        results.extend(_resource_results(app, workspace_id))
    if "artifact" in selected:
        results.extend(await asyncio.to_thread(_artifact_results, app, workspace_id))
    if "session" in selected:
        results.extend(_session_results(app, workspace_id))
    if "agent_run" in selected:
        results.extend(_agent_run_results(app, workspace_id))

    results = _disambiguate_duplicate_labels(results)
    needle = query.strip().casefold()
    if needle:
        results = [
            row
            for row in results
            if needle
            in " ".join(
                (str(row["label"]), str(row["detail"]), str(row["id"]), str(row["kind"]))
            ).casefold()
        ]
    return sorted(results, key=lambda row: (row["kind"], row["label"].casefold(), row["id"]))


__all__ = ["search_workspace_references"]
