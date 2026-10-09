"""Thread-safe response artifact buffering, independent of immutable minting."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.artifacts.records import ArtifactVersion

logger = logging.getLogger(__name__)

#: Per-session turn-scoped buffer of output artifact versions selected THIS turn —
#: the source for the one-``resource_link``-part-per-returned-artifact append at
#: turn finalize. Reconciliation observations remain in the registry and Evidence,
#: but never become answer attachments. Fresh produced versions and explicit
#: ``create_artifact`` reuse results land here; passive same-sha observations do not.
#: ``turn_finalize`` drains + filters by turn id
#: and clears the session's list; ``settle_failed_finalize`` calls
#: :func:`clear_turn_artifacts` on the failure path so a crashed turn cannot
#: re-emit its buffered parts when the same turn is retried. Bounded per session
#: so a pathological turn cannot grow it unboundedly.
_TURN_ARTIFACT_CAP = 256
_TURN_ARTIFACT_LOCK = threading.Lock()


def _record_turn_artifact(
    app: "FastAPI",
    sid: str,
    *,
    workspace_id: str,
    name: str,
    version: "ArtifactVersion",
    turn_id: str,
    purpose: str | None = None,
) -> None:
    """Buffer an explicit output version for the finalize ``resource_link`` append.

    Thread-safe: the observer mint runs on a worker thread while a finalize on the
    turn thread may drain concurrently. A single module lock guards the per-session
    list so an append never races a drain-and-clear. Best-effort — a buffering
    failure must never break a live mint, so the caller wraps the whole mint.
    """
    with _TURN_ARTIFACT_LOCK:
        buffers = getattr(app.state, "turn_artifacts", None)
        if buffers is None:
            buffers = {}
            app.state.turn_artifacts = buffers
        entries = buffers.setdefault(sid, [])
        artifact_id = str(getattr(version, "artifact_id", "") or "")
        for entry in entries:
            if (
                str(entry.get("turn_id") or "") == turn_id
                and str(getattr(entry.get("version"), "artifact_id", "") or "") == artifact_id
            ):
                if purpose is not None:
                    entry["purpose"] = purpose
                return
        if len(entries) >= _TURN_ARTIFACT_CAP:
            logger.warning(
                "artifact turn buffer at cap reason=turn_artifact_cap session=%s cap=%d",
                sid,
                _TURN_ARTIFACT_CAP,
            )
            return
        entries.append(
            {
                "workspace_id": workspace_id,
                "response_session_id": sid,
                "name": name,
                "version": version,
                "turn_id": turn_id,
                "purpose": purpose,
            }
        )


def drain_turn_artifacts(app: "FastAPI", sid: str, turn_id: str = "") -> list[dict[str, Any]]:
    """Pop the turn's buffered artifact versions for ``sid`` (finalize seam).

    Returns the buffered entries and CLEARS the session's list. When ``turn_id`` is
    given, only entries stamped with that turn are returned — a defensive filter so
    a stray mint from a prior (un-drained) turn never rides this turn's message;
    entries for other turns are dropped with the list (a new turn re-buffers its
    own). Empty list when nothing was minted this turn.
    """
    with _TURN_ARTIFACT_LOCK:
        buffers = getattr(app.state, "turn_artifacts", None)
        if not buffers:
            return []
        entries = buffers.pop(sid, [])
    if not turn_id:
        return entries
    return [e for e in entries if str(e.get("turn_id") or "") == turn_id]


def clear_turn_artifacts(app: "FastAPI", sid: str) -> None:
    """Drop the whole per-session turn buffer (failed-finalize seam, finding [7]).

    A finalize-region crash never reaches the finalize drain, so its buffered
    versions would linger. If the SAME turn is then retried, the retry re-buffers
    the same mints and the next successful finalize would drain BOTH the stale and
    the fresh entries — one artifact, two ``resource_link`` parts. Clearing the
    session's buffer on the failure path makes the retry emit exactly once. Called
    unconditionally from ``settle_failed_finalize``; a missing buffer is a no-op.
    """
    with _TURN_ARTIFACT_LOCK:
        buffers = getattr(app.state, "turn_artifacts", None)
        if buffers:
            buffers.pop(sid, None)
