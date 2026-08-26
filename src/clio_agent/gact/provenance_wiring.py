"""Provenance wiring installed at gact app assembly (#1247).

app.py stays assembly-only (the ``relay_wiring.py`` precedent its baseline
comment cites): the artifact-provenance backend construction, the centralized
session-lifecycle semantic bridge, and the shutdown close live here as owner
code, called from ``build_app`` / ``_lifespan`` as one-liners.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clio_agent.gact.runtime.globals import _emit_semantic_event


def wire_artifact_provenance(app: Any, store_root: Path) -> Any:
    """Build and stash the artifact-provenance backend; return it for the sink."""

    from clio_agent.gact.artifacts.provenance import (  # noqa: PLC0415
        build_artifact_provenance_backend,
    )

    backend = build_artifact_provenance_backend(app, store_root)
    app.state.artifact_provenance_backend = backend
    return backend


def install_session_lifecycle_observer(app: Any) -> None:
    """Bridge session lifecycle onto the ARC-first semantic highway.

    Every root, forked, imported, and child-agent session created through
    ``SessionStore`` reaches the same highway. Session deletion is the only
    terminal workflow signal; process shutdown deliberately leaves persistent
    sessions open.
    """

    def _observe_session_lifecycle(event_type: str, session: Any) -> None:
        _emit_semantic_event(
            app,
            str(session.id),
            event_type,
            trace_id=f"session:{session.id}",
            status="completed",
            summary=f"session {session.id} {event_type.rsplit('.', 1)[-1]}",
            actor={"role": "system"},
            subject={"session_id": session.id},
            payload={
                "workspace_id": session.workspace_id,
                "parent_session_id": session.parent_session_id,
                "agent": dict(session.agent),
                "started_at": session.created_at,
            },
        )

    app.state.sessions.set_lifecycle_observer(_observe_session_lifecycle)


def close_artifact_backend(app: Any) -> None:
    """Close the artifact-provenance backend at lifespan teardown (defensive)."""

    backend = getattr(app.state, "artifact_provenance_backend", None)
    close = getattr(backend, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # pragma: no cover - defensive shutdown cleanup  # noqa: BLE001,S110
            pass
