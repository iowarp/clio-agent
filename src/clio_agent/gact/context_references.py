"""Discovery and authorization of structured workspace references.

``context_ref`` parts are display-light handles supplied by a client.  This
module resolves every handle against the repositories already owned by the
active GACT app and replaces all display/provenance fields with server-owned
values before the part reaches the durable message ledger.

The model-facing DELIVERY of an admitted part lives in its own owner module,
:mod:`clio_agent.gact.context_reference_delivery`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent import conf
from clio_agent.gact.artifacts.registry import get_registry
from clio_agent.gact.artifacts.storage import resolve_owned_artifact_path
from clio_agent.gact.artifacts.wire import mime_for
from clio_agent.gact.context_reference_domain import (
    CONTEXT_REFERENCE_CAPABILITY,
    CONTEXT_REFERENCE_KINDS,
    REFERENCE_SEARCH_KINDS,
    SUMMARY_REFERENCE_KINDS,
    ContextReferenceError,
)
from clio_agent.gact.context_reference_domain import (
    WorkspaceSession as _WorkspaceSession,
)
from clio_agent.gact.context_reference_file_io import (
    file_media_type as _file_media_type,
)
from clio_agent.gact.context_reference_file_io import (
    sha256_file as _sha256_file,
)
from clio_agent.gact.types import Message, Part

if TYPE_CHECKING:
    from fastapi import FastAPI


def summary_message_limit() -> int:
    """How many recent messages a bounded session summary may carry.

    Config: ``gact.context_references.summary_messages`` /
    ``CLIO_CONTEXT_REFERENCE_SUMMARY_MESSAGES``.
    """

    return conf.resolve(
        "gact.context_references.summary_messages",
        env="CLIO_CONTEXT_REFERENCE_SUMMARY_MESSAGES",
        default=5,
        cast=conf.as_int,
    )


def summary_excerpt_chars() -> int:
    """Character ceiling for one excerpt inside a bounded reference summary.

    Config: ``gact.context_references.summary_excerpt_chars`` /
    ``CLIO_CONTEXT_REFERENCE_SUMMARY_EXCERPT_CHARS``.
    """

    return conf.resolve(
        "gact.context_references.summary_excerpt_chars",
        env="CLIO_CONTEXT_REFERENCE_SUMMARY_EXCERPT_CHARS",
        default=600,
        cast=conf.as_int,
    )


def max_hashable_bytes() -> int:
    """Byte ceiling on a workspace file admitted as a ``context_ref``.

    Admission digests the WHOLE file to pin its revision, so an unbounded
    reference to a multi-gigabyte artefact is a per-request read of that size.
    A file above this ceiling is REFUSED with a typed reason naming the limit --
    never silently admitted with a partial or skipped digest.

    Config: ``gact.context_references.max_hashable_bytes`` /
    ``CLIO_CONTEXT_REFERENCE_MAX_HASHABLE_BYTES``.
    """

    return conf.resolve(
        "gact.context_references.max_hashable_bytes",
        env="CLIO_CONTEXT_REFERENCE_MAX_HASHABLE_BYTES",
        default=64 * 1024 * 1024,
        cast=conf.as_int,
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


def _summary_excerpt(text: str) -> str:
    limit = summary_excerpt_chars()
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def _message_excerpt(message: Message) -> str:
    return _summary_excerpt(
        "\n".join(part.text for part in message.parts if part.type == "text" and part.text)
    )


def _session_summary(app: "FastAPI", session: _WorkspaceSession) -> dict[str, Any]:
    message_limit = summary_message_limit()
    excerpt_limit = summary_excerpt_chars()
    messages = list(app.state.messages.get(session.id, []))[-message_limit:]
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
            "message_limit": message_limit,
            "excerpt_char_limit": excerpt_limit,
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
        "error_reason": _summary_excerpt(str(task.error_reason or "")),
        "answer_excerpt": _summary_excerpt(str(result.get("answer_excerpt") or "")),
        "updated_at": task.updated_at,
        "provenance": {
            "source": "agent_task_repository",
            "bounded": True,
            "excerpt_char_limit": summary_excerpt_chars(),
        },
    }


def _refuse_unhashable_file(ref_id: str, workspace_id: str, size: int) -> None:
    """Refuse a workspace file too large to digest at admission.

    Pinning a ``workspace_file`` revision means digesting the WHOLE file, so an
    unbounded reference turns one POST into a read of that many bytes on a
    request path. Past the ceiling the reference is REFUSED with the limit and
    the actual size in the payload -- never admitted with a skipped or partial
    digest, which would make the pinned revision a lie.
    """

    limit = max_hashable_bytes()
    if size <= limit:
        return
    raise _failure(
        status_code=413,
        error="context_ref_too_large",
        message="workspace file is larger than the referenceable size limit",
        ref_kind="workspace_file",
        ref_id=ref_id,
        workspace_id=workspace_id,
        recoverable=True,
        size_bytes=size,
        max_bytes=limit,
        recovery_actions=["reference_a_smaller_file", "raise_max_hashable_bytes"],
    )


def _resolve_file(
    app: "FastAPI", workspace_id: str, part: Part, *, enforce_revision: bool
) -> tuple[Part, dict[str, Any]]:
    _root, path = _contained_file(app, workspace_id, part.ref_id)
    requested = part.revision
    try:
        stat_revision = _stat_revision(path)
        size = path.stat().st_size
        _refuse_unhashable_file(part.ref_id, workspace_id, size)
        actual_sha = _sha256_file(path)
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
        "navigation": {"workspace_id": workspace_id, "path": part.ref_id},
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
        "navigation": {
            "workspace_id": workspace_id,
            "artifact_id": version.artifact_id,
        },
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
        "navigation": {"workspace_id": workspace_id, "session_id": target.id},
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
        "navigation": {
            "workspace_id": workspace_id,
            "task_id": task.task_id,
            "session_id": task.child_session_id,
        },
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


def _resolve_evidence_snapshot(
    app: "FastAPI",
    workspace_id: str,
    part: Part,
    *,
    enforce_revision: bool,
) -> tuple[Part, dict[str, Any]]:
    from clio_agent.gact.context_reference_evidence import (  # noqa: PLC0415
        find_evidence_reference_snapshot,
    )

    found = find_evidence_reference_snapshot(app, workspace_id, part.ref_kind, part.ref_id)
    if found is None:
        raise _failure(
            status_code=404,
            error="context_ref_inaccessible",
            message="evidence reference is missing or outside the active workspace",
            ref_kind=part.ref_kind,
            ref_id=part.ref_id,
            workspace_id=workspace_id,
            recoverable=True,
        )
    reference, payload = found
    actual_revision = str(reference["revision"])
    if not part.revision:
        raise _failure(
            status_code=400,
            error="context_ref_revision_required",
            message="evidence references must pin the selected snapshot revision",
            ref_kind=part.ref_kind,
            ref_id=part.ref_id,
            recoverable=True,
            actual_revision=actual_revision,
        )
    if enforce_revision and part.revision != actual_revision:
        raise _failure(
            status_code=409,
            error="context_ref_stale",
            message="evidence changed after the reference was selected",
            ref_kind=part.ref_kind,
            ref_id=part.ref_id,
            recoverable=True,
            requested_revision=part.revision,
            actual_revision=actual_revision,
        )
    summary = {
        "kind": part.ref_kind,
        "id": part.ref_id,
        "label": reference["label"],
        "detail": reference["detail"],
        "snapshot": payload,
    }
    metadata = {
        "kind": part.ref_kind,
        "ref_id": part.ref_id,
        "workspace_id": workspace_id,
        "requested_revision": part.revision,
        "resolved_revision": actual_revision,
        "media_type": reference["media_type"],
        "navigation": reference["navigation"],
        "delivery": {"mode": "bounded_summary", "summary": summary},
        "provenance": {
            "source": "session_evidence_repository",
            "session_id": reference["navigation"].get("session_id", ""),
        },
    }
    resolved = part.model_copy(
        update={
            "label": str(reference["label"]),
            "revision": actual_revision,
            "metadata": {**dict(part.metadata), "context_reference": metadata},
        }
    )
    return resolved, metadata


def resource_part_from_reference(app: "FastAPI", workspace_id: str, part: Part) -> Part:
    """Turn a ``context_ref{ref_kind: resource}`` handle into a ``resource_ref`` part.

    ``resource`` is a SEARCHABLE kind (the picker lists uploaded sources) but not a
    context-reference kind, so picking a resource row used to 400 with
    ``context_ref_kind_invalid`` -- a row the server offered and then refused. It
    is not a new attachment mechanism either: the composer already delivers an
    uploaded source as a ``resource_ref`` part with its own custody, revision and
    per-model delivery planning. So admission MAPS the picked handle onto that
    existing part type, revision-checked against the custody record, and the rest
    of the pipeline is unchanged.
    """

    store = getattr(app.state, "resource_store", None)
    record = store.get(workspace_id, part.ref_id) if store is not None else None
    if record is None:
        raise _failure(
            status_code=404,
            error="context_ref_inaccessible",
            message="resource reference is missing or outside the active workspace",
            ref_kind="resource",
            ref_id=part.ref_id,
            workspace_id=workspace_id,
            recoverable=True,
        )
    actual_revision = str(record.revision)
    if part.revision and part.revision != actual_revision:
        raise _failure(
            status_code=409,
            error="context_ref_stale",
            message="resource reference does not name the current immutable revision",
            ref_kind="resource",
            ref_id=part.ref_id,
            recoverable=True,
            requested_revision=part.revision,
            actual_revision=actual_revision,
        )
    return Part(
        type="resource_ref",
        id=part.id,
        resource_id=record.id,
        resource_revision=actual_revision,
        name=record.name,
        media_type=record.detected_mime,
        metadata={
            **dict(part.metadata),
            "picked_as": {"ref_kind": "resource", "ref_id": part.ref_id},
        },
    )


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
    if part.ref_kind == "agent_run":
        return _resolve_agent_run(app, workspace_id, part)
    return _resolve_evidence_snapshot(app, workspace_id, part, enforce_revision=enforce_revision)


def authorize_context_reference_parts_sync(
    app: "FastAPI", session: _WorkspaceSession, parts: Iterable[Part]
) -> list[Part]:
    """Synchronously authorize and canonicalize every ``context_ref`` before persistence."""

    resolved: list[Part] = []
    for part in parts:
        if part.type != "context_ref":
            resolved.append(part)
            continue
        try:
            if part.ref_kind == "resource":
                resolved.append(resource_part_from_reference(app, session.workspace_id, part))
                continue
            canonical, _metadata = _resolve_part_sync(
                app,
                session.workspace_id,
                part,
                enforce_revision=True,
            )
        except ContextReferenceError as exc:
            raise exc.http_exception() from exc
        resolved.append(canonical)
    return resolved


async def authorize_context_reference_parts(
    app: "FastAPI", session: _WorkspaceSession, parts: Iterable[Part]
) -> list[Part]:
    """Authorize references off the event loop using the synchronous admission seam."""

    return await asyncio.to_thread(
        authorize_context_reference_parts_sync, app, session, list(parts)
    )


__all__ = [
    "CONTEXT_REFERENCE_CAPABILITY",
    "CONTEXT_REFERENCE_KINDS",
    "REFERENCE_SEARCH_KINDS",
    "SUMMARY_REFERENCE_KINDS",
    "ContextReferenceError",
    "authorize_context_reference_parts",
    "authorize_context_reference_parts_sync",
    "max_hashable_bytes",
    "resource_part_from_reference",
    "summary_excerpt_chars",
    "summary_message_limit",
]
