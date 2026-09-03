"""Cross-repository discovery for structured workspace references."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent import conf
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.wire import mime_for
from clio_agent.gact.context_reference_evidence import evidence_reference_snapshots
from clio_agent.gact.context_reference_result import reference_result as _reference_result
from clio_agent.gact.routes.workspace_file_policy import (
    is_internal_workspace_file_directory,
    skip_workspace_file_directory,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def empty_query_limit_per_kind() -> int:
    """Rows returned per kind when the picker opens with no query typed.

    Config: ``gact.context_references.browse_limit_per_kind`` /
    ``CLIO_CONTEXT_REFERENCE_BROWSE_LIMIT``.
    """

    return conf.resolve(
        "gact.context_references.browse_limit_per_kind",
        env="CLIO_CONTEXT_REFERENCE_BROWSE_LIMIT",
        default=20,
        cast=conf.as_int,
    )


def search_limit() -> int:
    """Total rows one reference search may return for a typed query.

    Config: ``gact.context_references.search_limit`` /
    ``CLIO_CONTEXT_REFERENCE_SEARCH_LIMIT``.
    """

    return conf.resolve(
        "gact.context_references.search_limit",
        env="CLIO_CONTEXT_REFERENCE_SEARCH_LIMIT",
        default=100,
        cast=conf.as_int,
    )


def _degradation(app: "FastAPI", workspace_id: str, reason: str, detail: str) -> None:
    """Record one typed discovery degradation on the app's quarantine surface.

    Mirrors the composer's resource-store quarantine reporting: a repository that
    cannot be listed makes the picker show FEWER rows than exist, and a client
    entitled to know why must not be told "there is nothing here".
    """

    logger.warning(
        "reference discovery degraded reason=%s workspace=%s detail=%s",
        reason,
        workspace_id,
        detail,
    )
    rows = getattr(app.state, "reference_search_degradations", None)
    if rows is None:
        rows = []
        app.state.reference_search_degradations = rows
    row = {"reason": reason, "workspace_id": workspace_id, "detail": detail}
    if row not in rows:
        rows.append(row)


def _walk_workspace_files(
    app: "FastAPI",
    workspace_id: str,
    *,
    needle: str = "",
    limit: int = 0,
) -> list[dict[str, Any]]:
    from clio_agent.gact.context_references import (  # noqa: PLC0415
        _file_media_type,
        _stat_revision,
        _workspace_root,
    )

    limit = limit or search_limit()
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
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # A store that cannot be listed is a REAL capability statement: the picker
        # will show no uploaded sources, and returning [] silently told the client
        # none had ever been uploaded.
        _degradation(
            app,
            workspace_id,
            "resource_store_unreadable",
            f"{type(exc).__name__}: {exc}",
        )
        return []
    readable_names_by_hash = {
        str(getattr(record, "sha256", "") or "").casefold()
        for record in records
        if not _is_content_addressed_name(
            str(getattr(record, "name", "") or ""),
            str(getattr(record, "sha256", "") or ""),
        )
    }
    results: list[dict[str, Any]] = []
    for record in records:
        ref_id = str(getattr(record, "id", "") or "")
        label = str(getattr(record, "name", "") or ref_id)
        content_hash = str(getattr(record, "sha256", "") or "").casefold()
        if content_hash in readable_names_by_hash and _is_content_addressed_name(
            label, content_hash
        ):
            # A duplicate upload whose filename is only its content hash is hidden
            # behind the readable sibling naming the SAME bytes. That is a row the
            # user cannot see, so it is reported rather than silently dropped.
            _degradation(
                app,
                workspace_id,
                "resource_content_addressed_name_hidden",
                f"resource {ref_id} is a content-addressed duplicate of sha256:{content_hash}",
            )
            continue
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
                detail="Uploaded source",
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


def _is_content_addressed_name(name: str, content_hash: str) -> bool:
    """Return whether a filename exposes its content hash instead of a useful name."""

    stem = Path(name).stem.casefold()
    return bool(content_hash and stem == content_hash)


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

    needle = query.strip().casefold()
    limit = search_limit()
    browse_limit = empty_query_limit_per_kind()
    # EVERY producer walks a repository: a filesystem tree, a durable resource
    # index, the session/message ledgers, or -- most expensive of all -- the
    # evidence snapshot builder, which folds every message part of every session
    # in the workspace. Two of them were already off-loop and the rest were not,
    # so a search on a busy workspace stalled the whole server. They now run in
    # ONE worker thread (not one per kind: they share the same in-memory state and
    # fanning them out would only multiply the GIL churn).
    results: list[dict[str, Any]] = await asyncio.to_thread(
        _collect_reference_results,
        app,
        workspace_id,
        selected=selected,
        needle=needle,
        limit=limit,
        browse_limit=browse_limit,
    )
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
        return ordered[:limit]

    counts: dict[str, int] = {}
    bounded: list[dict[str, Any]] = []
    for row in ordered:
        kind = str(row["kind"])
        count = counts.get(kind, 0)
        if count >= browse_limit:
            continue
        counts[kind] = count + 1
        bounded.append(row)
    return bounded


def _collect_reference_results(
    app: "FastAPI",
    workspace_id: str,
    *,
    selected: set[str],
    needle: str,
    limit: int,
    browse_limit: int,
) -> list[dict[str, Any]]:
    """Run every repository producer for one search (worker thread, never the loop)."""

    results: list[dict[str, Any]] = []
    if "workspace_file" in selected:
        results.extend(
            _walk_workspace_files(
                app,
                workspace_id,
                needle=needle,
                limit=limit if needle else browse_limit,
            )
        )
    if "resource" in selected:
        results.extend(_resource_results(app, workspace_id))
    if "artifact" in selected:
        results.extend(_artifact_results(app, workspace_id))
    if "session" in selected:
        results.extend(_session_results(app, workspace_id))
    if "agent_run" in selected:
        results.extend(_agent_run_results(app, workspace_id))
    evidence_kinds = selected & {"evidence_source", "context_frame", "diff", "plan"}
    if evidence_kinds:
        results.extend(
            row for row, _payload in evidence_reference_snapshots(app, workspace_id, evidence_kinds)
        )
    return results


__all__ = [
    "empty_query_limit_per_kind",
    "search_limit",
    "search_workspace_references",
]
