"""Discovery, authorization, and delivery of structured workspace references.

``context_ref`` parts are display-light handles supplied by a client.  This
module resolves every handle against the repositories already owned by the
active GACT app and replaces all display/provenance fields with server-owned
values before the part reaches the durable message ledger.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.storage import resolve_owned_artifact_path
from clio_agent.gact.artifacts.wire import mime_for
from clio_agent.gact.routes.workspace_file_policy import (
    is_internal_workspace_file_directory,
    skip_workspace_file_directory,
)
from clio_agent.gact.runtime.constants import _CTX_MAX_BYTES
from clio_agent.gact.runtime.globals import _ContextFileAccessError
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo, Message, Part

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.sessions import Session

ContextReferenceKind = Literal["workspace_file", "artifact", "session", "agent_run"]
ReferenceSearchKind = Literal["workspace_file", "resource", "artifact", "session", "agent_run"]

CONTEXT_REFERENCE_KINDS: frozenset[str] = frozenset(
    {"workspace_file", "artifact", "session", "agent_run"}
)
REFERENCE_SEARCH_KINDS: frozenset[str] = frozenset({*CONTEXT_REFERENCE_KINDS, "resource"})
_SEARCH_LIMIT = 5000
_SUMMARY_MESSAGE_LIMIT = 5
_SUMMARY_EXCERPT_CHARS = 600
_FILE_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class ContextReferenceError(Exception):
    """Typed failure raised while resolving a client-supplied reference."""

    status_code: int
    error: str
    message: str
    details: dict[str, Any]
    recoverable: bool = False

    def http_exception(self) -> Exception:
        """Project this domain failure to the server's typed HTTP envelope."""

        from fastapi import HTTPException  # noqa: PLC0415

        return HTTPException(
            status_code=self.status_code,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error=self.error,
                    message=self.message,
                    details=self.details,
                    recoverable=self.recoverable,
                )
            ).model_dump(exclude_none=True),
        )


def _failure(
    *,
    status_code: int,
    error: str,
    message: str,
    ref_kind: str,
    ref_id: str,
    recoverable: bool = False,
    **details: Any,
) -> ContextReferenceError:
    return ContextReferenceError(
        status_code=status_code,
        error=error,
        message=message,
        details={"ref_kind": ref_kind, "ref_id": ref_id, **details},
        recoverable=recoverable,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_FILE_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _file_media_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _workspace_root(app: "FastAPI", workspace_id: str) -> Path:
    workspace = app.state.workspaces.get(workspace_id)
    if workspace is None:
        raise _failure(
            status_code=404,
            error="not_found",
            message=f"workspace not found: {workspace_id}",
            ref_kind="workspace",
            ref_id=workspace_id,
        )
    return Path(workspace.root_path or os.getcwd()).expanduser().resolve(strict=False)


def _contained_file(app: "FastAPI", workspace_id: str, ref_id: str) -> tuple[Path, Path]:
    root = _workspace_root(app, workspace_id)
    if not ref_id.strip() or Path(ref_id).is_absolute():
        raise _failure(
            status_code=400,
            error="context_ref_invalid",
            message="workspace_file ref_id must be a non-empty workspace-relative path",
            ref_kind="workspace_file",
            ref_id=ref_id,
            recoverable=True,
        )
    target = (root / ref_id).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise _failure(
            status_code=403,
            error="context_ref_inaccessible",
            message="workspace file reference escapes the active workspace",
            ref_kind="workspace_file",
            ref_id=ref_id,
            workspace_id=workspace_id,
        ) from exc
    if not target.is_file():
        raise _failure(
            status_code=404,
            error="context_ref_inaccessible",
            message="workspace file reference is missing or inaccessible",
            ref_kind="workspace_file",
            ref_id=ref_id,
            workspace_id=workspace_id,
            recoverable=True,
        )
    return root, target


def _stat_revision(path: Path) -> str:
    stat = path.stat()
    return f"stat:{stat.st_mtime_ns}:{stat.st_size}"


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


def _walk_workspace_files(app: "FastAPI", workspace_id: str) -> list[dict[str, Any]]:
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


def _summary_excerpt(text: str) -> str:
    clean = " ".join(text.split())
    if len(clean) <= _SUMMARY_EXCERPT_CHARS:
        return clean
    return clean[: _SUMMARY_EXCERPT_CHARS - 1] + "…"


def _message_excerpt(message: Message) -> str:
    return _summary_excerpt(
        "\n".join(part.text for part in message.parts if part.type == "text" and part.text)
    )


def _session_summary(app: "FastAPI", session: "Session") -> dict[str, Any]:
    messages = list(app.state.messages.get(session.id, []))[-_SUMMARY_MESSAGE_LIMIT:]
    return {
        "session_id": session.id,
        "title": session.title,
        "status": session.status,
        "updated_at": session.updated_at,
        "message_count": session.message_count,
        "recent": [
            {"message_id": message.id, "role": message.role, "excerpt": _message_excerpt(message)}
            for message in messages
            if _message_excerpt(message)
        ],
        "provenance": {
            "source": "session_message_repository",
            "bounded": True,
            "message_limit": _SUMMARY_MESSAGE_LIMIT,
            "excerpt_char_limit": _SUMMARY_EXCERPT_CHARS,
        },
    }


def _agent_run_summary(task: Any) -> dict[str, Any]:
    result = task.result if isinstance(task.result, Mapping) else {}
    return {
        "task_id": task.task_id,
        "parent_session_id": task.parent_session_id,
        "child_session_id": task.child_session_id,
        "status": task.status,
        "expert_id": str((task.agent_ref or {}).get("expert_id") or ""),
        "error_reason": task.error_reason,
        "answer_excerpt": _summary_excerpt(str(result.get("answer_excerpt") or "")),
        "updated_at": task.updated_at,
        "provenance": {
            "source": "agent_task_repository",
            "bounded": True,
            "excerpt_char_limit": _SUMMARY_EXCERPT_CHARS,
        },
    }


def _resolve_file(
    app: "FastAPI", workspace_id: str, part: Part, *, enforce_revision: bool
) -> tuple[Part, dict[str, Any]]:
    _root, path = _contained_file(app, workspace_id, part.ref_id)
    requested = part.revision
    try:
        stat_revision = _stat_revision(path)
        actual_sha = _sha256_file(path)
        size = path.stat().st_size
    except OSError as exc:
        raise _failure(
            status_code=404,
            error="context_ref_inaccessible",
            message="workspace file could not be read",
            ref_kind="workspace_file",
            ref_id=part.ref_id,
            workspace_id=workspace_id,
            recoverable=True,
            operation="read",
            original_error=type(exc).__name__,
        ) from exc
    actual_revision = f"sha256:{actual_sha}"
    if enforce_revision and requested and requested not in {stat_revision, actual_revision}:
        raise _failure(
            status_code=409,
            error="context_ref_stale",
            message="workspace file changed after the reference was selected",
            ref_kind="workspace_file",
            ref_id=part.ref_id,
            recoverable=True,
            requested_revision=requested,
            actual_revision=actual_revision,
            actual_stat_revision=stat_revision,
        )
    media_type = _file_media_type(path)
    metadata = {
        "kind": "workspace_file",
        "ref_id": part.ref_id,
        "workspace_id": workspace_id,
        "requested_revision": requested,
        "resolved_revision": actual_revision,
        "media_type": media_type,
        "delivery": {
            "mode": "inline_text" if media_type.startswith("text/") else "metadata",
            "sha256": actual_sha,
            "size_bytes": size,
        },
        "provenance": {"source": "workspace_file_repository", "path": part.ref_id},
    }
    resolved = part.model_copy(
        update={
            "label": path.name,
            "revision": actual_revision,
            "metadata": {**dict(part.metadata), "context_reference": metadata},
        }
    )
    return resolved, metadata


def _artifact_path(app: "FastAPI", record: Any, version: Any) -> Path | None:
    root = _workspace_root(app, record.workspace_id)
    owned = resolve_owned_artifact_path(app, version, workspace_root=root)
    if owned is not None and owned.is_file():
        return owned
    if not version.path:
        return None
    candidate = Path(version.path).expanduser().resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _resolve_artifact(app: "FastAPI", workspace_id: str, part: Part) -> tuple[Part, dict[str, Any]]:
    found = get_registry(app).get_by_artifact_id(part.ref_id)
    if found is None:
        raise _failure(
            status_code=404,
            error="context_ref_inaccessible",
            message="artifact reference is missing or inaccessible",
            ref_kind="artifact",
            ref_id=part.ref_id,
            recoverable=True,
        )
    record, version = found
    if record.workspace_id != workspace_id:
        raise _failure(
            status_code=403,
            error="context_ref_inaccessible",
            message="artifact does not belong to the active workspace",
            ref_kind="artifact",
            ref_id=part.ref_id,
            workspace_id=workspace_id,
        )
    actual_revision = f"v{version.version}"
    if not part.revision:
        raise _failure(
            status_code=400,
            error="context_ref_revision_required",
            message="artifact references must pin an immutable revision",
            ref_kind="artifact",
            ref_id=part.ref_id,
            recoverable=True,
            actual_revision=actual_revision,
        )
    if part.revision != actual_revision:
        raise _failure(
            status_code=409,
            error="context_ref_stale",
            message="artifact reference revision does not match its immutable version",
            ref_kind="artifact",
            ref_id=part.ref_id,
            recoverable=True,
            requested_revision=part.revision,
            actual_revision=actual_revision,
        )
    path = _artifact_path(app, record, version)
    try:
        actual_sha = _sha256_file(path) if path is not None else ""
    except OSError as exc:
        raise _failure(
            status_code=404,
            error="context_ref_inaccessible",
            message="artifact bytes could not be read",
            ref_kind="artifact",
            ref_id=part.ref_id,
            workspace_id=workspace_id,
            recoverable=True,
            operation="read",
            original_error=type(exc).__name__,
        ) from exc
    recorded_sha = str(version.sha256 or "")
    if actual_sha and recorded_sha and actual_sha != recorded_sha:
        raise _failure(
            status_code=409,
            error="context_ref_stale",
            message="artifact bytes no longer match the pinned revision",
            ref_kind="artifact",
            ref_id=part.ref_id,
            recoverable=True,
            requested_revision=actual_revision,
            recorded_sha256=recorded_sha,
            actual_sha256=actual_sha,
        )
    media_type = mime_for(version, record.name)
    metadata = {
        "kind": "artifact",
        "ref_id": part.ref_id,
        "workspace_id": workspace_id,
        "requested_revision": part.revision,
        "resolved_revision": actual_revision,
        "media_type": media_type,
        "delivery": {
            "mode": "inline_text" if media_type.startswith("text/") and path else "metadata",
            "sha256": actual_sha or recorded_sha,
            "size_bytes": version.size_bytes,
        },
        "provenance": {
            "source": "artifact_registry",
            "artifact_id": version.artifact_id,
            "name": record.name,
            "version": version.version,
            "custody": version.custody.value,
            "producer": dict(version.producer or {}),
        },
    }
    resolved = part.model_copy(
        update={
            "label": record.name,
            "revision": actual_revision,
            "metadata": {**dict(part.metadata), "context_reference": metadata},
        }
    )
    return resolved, metadata


def _resolve_session(app: "FastAPI", workspace_id: str, part: Part) -> tuple[Part, dict[str, Any]]:
    target = app.state.sessions.get(part.ref_id)
    if target is None or target.workspace_id != workspace_id:
        raise _failure(
            status_code=403 if target is not None else 404,
            error="context_ref_inaccessible",
            message="session reference is missing or outside the active workspace",
            ref_kind="session",
            ref_id=part.ref_id,
            workspace_id=workspace_id,
        )
    summary = _session_summary(app, target)
    metadata = {
        "kind": "session",
        "ref_id": target.id,
        "workspace_id": workspace_id,
        "requested_revision": part.revision,
        "resolved_revision": target.updated_at,
        "media_type": "application/vnd.clio.session-summary+json",
        "delivery": {"mode": "bounded_summary", "summary": summary},
        "provenance": summary["provenance"],
    }
    resolved = part.model_copy(
        update={
            "label": target.title or target.id,
            "revision": target.updated_at,
            "metadata": {**dict(part.metadata), "context_reference": metadata},
        }
    )
    return resolved, metadata


def _resolve_agent_run(
    app: "FastAPI", workspace_id: str, part: Part
) -> tuple[Part, dict[str, Any]]:
    registry = getattr(app.state, "agent_task_registry", None)
    task = registry.get(part.ref_id) if registry is not None else None
    child = app.state.sessions.get(task.child_session_id) if task is not None else None
    parent = app.state.sessions.get(task.parent_session_id) if task is not None else None
    owner = child or parent
    if task is None or owner is None or owner.workspace_id != workspace_id:
        raise _failure(
            status_code=403 if task is not None else 404,
            error="context_ref_inaccessible",
            message="agent run reference is missing or outside the active workspace",
            ref_kind="agent_run",
            ref_id=part.ref_id,
            workspace_id=workspace_id,
        )
    summary = _agent_run_summary(task)
    expert = str((task.agent_ref or {}).get("expert_id") or "agent")
    label = task.run_label or f"{expert} #{task.run_index + 1}"
    metadata = {
        "kind": "agent_run",
        "ref_id": task.task_id,
        "workspace_id": workspace_id,
        "requested_revision": part.revision,
        "resolved_revision": task.updated_at,
        "media_type": "application/vnd.clio.agent-run-summary+json",
        "delivery": {"mode": "task_summary", "summary": summary},
        "provenance": summary["provenance"],
    }
    resolved = part.model_copy(
        update={
            "label": label,
            "revision": task.updated_at,
            "metadata": {**dict(part.metadata), "context_reference": metadata},
        }
    )
    return resolved, metadata


def _resolve_part_sync(
    app: "FastAPI", workspace_id: str, part: Part, *, enforce_revision: bool
) -> tuple[Part, dict[str, Any]]:
    if part.ref_kind not in CONTEXT_REFERENCE_KINDS:
        raise _failure(
            status_code=400,
            error="context_ref_kind_invalid",
            message=f"unsupported context reference kind: {part.ref_kind or '<empty>'}",
            ref_kind=part.ref_kind,
            ref_id=part.ref_id,
            recoverable=True,
            allowed_kinds=sorted(CONTEXT_REFERENCE_KINDS),
        )
    if not part.ref_id:
        raise _failure(
            status_code=400,
            error="context_ref_invalid",
            message="context_ref requires ref_id; labels are display-only",
            ref_kind=part.ref_kind,
            ref_id=part.ref_id,
            recoverable=True,
        )
    if part.ref_kind == "workspace_file":
        return _resolve_file(app, workspace_id, part, enforce_revision=enforce_revision)
    if part.ref_kind == "artifact":
        return _resolve_artifact(app, workspace_id, part)
    if part.ref_kind == "session":
        return _resolve_session(app, workspace_id, part)
    return _resolve_agent_run(app, workspace_id, part)


async def authorize_context_reference_parts(
    app: "FastAPI", session: "Session", parts: Iterable[Part]
) -> list[Part]:
    """Authorize and canonicalize every ``context_ref`` before persistence."""

    resolved: list[Part] = []
    for part in parts:
        if part.type != "context_ref":
            resolved.append(part)
            continue
        try:
            canonical, _metadata = await asyncio.to_thread(
                _resolve_part_sync,
                app,
                session.workspace_id,
                part,
                enforce_revision=True,
            )
        except ContextReferenceError as exc:
            raise exc.http_exception() from exc
        resolved.append(canonical)
    return resolved


def enrich_with_context_references(
    app: "FastAPI", sid: str, user_text: str, user_message: Message
) -> str:
    """Resolve typed parts again and prepend their bounded model-facing content."""

    session = app.state.sessions.get(sid)
    if session is None:
        return user_text
    blocks: list[str] = []
    deliveries: list[dict[str, Any]] = []
    try:
        for part in user_message.parts:
            if part.type != "context_ref":
                continue
            canonical, metadata = _resolve_part_sync(
                app, session.workspace_id, part, enforce_revision=True
            )
            deliveries.append(metadata)
            delivery = metadata["delivery"]
            if canonical.ref_kind in {"session", "agent_run"}:
                summary = delivery["summary"]
                blocks.append(
                    f"### {canonical.ref_kind}: {canonical.label} [{canonical.ref_id}]\n"
                    f"{json.dumps(summary, sort_keys=True)}"
                )
                continue
            if delivery["mode"] == "inline_text":
                content_path: Path | None
                if canonical.ref_kind == "workspace_file":
                    _root, content_path = _contained_file(
                        app, session.workspace_id, canonical.ref_id
                    )
                else:
                    found = get_registry(app).get_by_artifact_id(canonical.ref_id)
                    assert found is not None
                    content_path = _artifact_path(app, found[0], found[1])
                if content_path is not None:
                    data = content_path.read_bytes()
                    text = data[:_CTX_MAX_BYTES].decode("utf-8", errors="replace")
                    suffix = (
                        f"\n... ({len(data) - _CTX_MAX_BYTES} more bytes truncated)"
                        if len(data) > _CTX_MAX_BYTES
                        else ""
                    )
                    blocks.append(
                        f"### {canonical.ref_kind}: {canonical.label} "
                        f"[{canonical.ref_id}@{canonical.revision}]\n```\n{text}{suffix}\n```"
                    )
                    continue
            blocks.append(
                f"### {canonical.ref_kind}: {canonical.label} "
                f"[{canonical.ref_id}@{canonical.revision}]\n"
                f"media_type={metadata['media_type']}; delivery=metadata-only; "
                f"sha256={delivery.get('sha256', '')}"
            )
    except (ContextReferenceError, OSError) as exc:
        if isinstance(exc, ContextReferenceError):
            info = ErrorInfo(
                error=exc.error,
                message=exc.message,
                details=exc.details,
                recoverable=exc.recoverable,
            )
        else:
            info = ErrorInfo(
                error="context_ref_inaccessible",
                message="context reference could not be read for delivery",
                details={"operation": "read", "original_error": type(exc).__name__},
                recoverable=True,
            )
        raise _ContextFileAccessError(info) from exc
    if not blocks:
        return user_text
    user_message.metadata["context_reference_deliveries"] = deliveries
    return (
        "## Structured context references (server-resolved)\n\n"
        + "\n\n".join(blocks)
        + "\n\n## User question\n\n"
        + user_text
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


__all__ = [
    "CONTEXT_REFERENCE_KINDS",
    "REFERENCE_SEARCH_KINDS",
    "ContextReferenceError",
    "authorize_context_reference_parts",
    "context_reference_frame_items",
    "enrich_with_context_references",
    "search_workspace_references",
]
