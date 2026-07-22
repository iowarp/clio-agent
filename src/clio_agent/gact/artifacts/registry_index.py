"""SessionStore badge index — a fast per-session projection OF the registry.

Owner module (no-accretion ground rule): the small bounded ``metadata.artifacts``
badge stamped onto a session is a fast index for the UI, NEVER a rebuild source
(owner decision #966.8 / #966.4 — the full set always rebuilds from the event log).
Split out of :mod:`clio_agent.gact.artifacts.registry` so the registry file owns the
projection + fold + mint and this owns the badge stamp/read-back.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.registry import ArtifactRegistry

logger = logging.getLogger(__name__)

# Bounded SessionStore badge index: at most this many named artifacts per session
# before it truncates (``names_truncated=True``). Badges only; never a rebuild source.
_SESSION_INDEX_NAME_CAP = 64


def build_session_index(registry: "ArtifactRegistry", workspace_id: str) -> dict[str, Any]:
    """Build the bounded per-workspace badge index.

    Shape ``{count, names: {name: {v, id, kind}}, names_truncated}`` — small, bounded
    (:data:`_SESSION_INDEX_NAME_CAP` names). The full set always rebuilds from the
    event log; this is never read back as a source (owner decision #966.8 / #966.4).
    """
    records = sorted(registry.list_for_workspace(workspace_id), key=lambda r: r.name)
    names: dict[str, Any] = {}
    truncated = False
    for record in records:
        head = record.head
        if head is None:
            continue
        if len(names) >= _SESSION_INDEX_NAME_CAP:
            truncated = True
            break
        names[record.name] = {
            "v": head.version,
            "id": head.artifact_id,
            "kind": head.kind.value,
        }
    return {"count": len(records), "names": names, "names_truncated": truncated}


def patch_session_index(
    app: "FastAPI", sid: str, registry: "ArtifactRegistry", workspace_id: str
) -> None:
    """Stamp the bounded badge index onto the session's metadata (best-effort)."""
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None:
        return
    index = build_session_index(registry, workspace_id)
    try:
        store.update(sid, metadata_patch={"artifacts": index})
    except Exception:  # noqa: BLE001 — a badge stamp must never break a turn
        logger.warning(
            "artifact session-index stamp skipped reason=store_update_failed sid=%s", sid
        )


def rehydrate_session_index(app: "FastAPI", sid: str) -> dict[str, Any]:
    """Read back a session's stored badge index (badges only), ``{}`` if none."""
    store = getattr(app.state, "sessions", None) or getattr(app.state, "session_store", None)
    if store is None:
        return {}
    session = store.get(sid)
    if session is None:
        return {}
    index = session.metadata.get("artifacts")
    return dict(index) if isinstance(index, dict) else {}


__all__ = [
    "build_session_index",
    "patch_session_index",
    "rehydrate_session_index",
]
