"""Provenance wiring installed at gact app assembly (#1247).

app.py stays assembly-only: the artifact-provenance backend construction, the centralized
session-lifecycle semantic bridge, and the shutdown close live here as owner
code, called from ``build_app`` / ``_lifespan`` as one-liners.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from clio_agent.gact.off_loop import schedule_off_loop
from clio_agent.gact.runtime.globals import _emit_semantic_event
from clio_agent.gact.semantic_events import semantic_event_from_events_content

if TYPE_CHECKING:
    from clio_agent.gact.sessions import Session


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

    def _observe_session_lifecycle(event_type: str, session: Session) -> None:
        if session.creation_digest:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RuntimeError("Trusted lifecycle confirmation requires an off-loop owner")

        # #1334: ``SessionStore.create/delete`` are called inline by async routes; the
        # event's store RPC must not run on the loop thread, so it is dispatched off it
        # when a loop runs here (inline otherwise), failures audited + logged.
        def _emit() -> None:
            if event_type == "session.created" and session.creation_digest:
                # A prior publication may be durable even when its reply or
                # derived notification failed. Read its existing Core evidence;
                # do not repeat the provenance/hook fanout merely to reuse a row.
                arc = getattr(app.state, "arc", None)
                if arc is None:
                    raise RuntimeError("Trusted session creation requires its Core provenance")
                if getattr(app.state, "semantic_event_sink", None) is None:
                    raise RuntimeError("Trusted session creation requires its provenance sink")
                for segment in arc._live.iter_session_event_segments(str(session.id)):
                    event = semantic_event_from_events_content(
                        segment.content, session_id=str(session.id)
                    )
                    if event.event_type != event_type:
                        continue
                    expected = {
                        "workspace_id": session.workspace_id,
                        "parent_session_id": session.parent_session_id,
                        "agent": dict(session.agent),
                        "started_at": session.created_at,
                    }
                    if event.trace_id != f"session:{session.id}" or any(
                        event.payload.get(key) != value for key, value in expected.items()
                    ):
                        raise ValueError("Stored session creation provenance changed")
                    return
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

        schedule_off_loop(_emit, label=f"session.lifecycle:{event_type}")

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
