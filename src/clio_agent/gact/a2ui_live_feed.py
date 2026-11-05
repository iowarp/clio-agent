"""A surface fed by a background script: state, not transcript (taskable run_code CT10).

An agent revising a view in its own turn writes a transcript part per update, as before.
A background script feeding a surface for hours must not: about three transcript parts an
update, measured. When ``update_a2ui_data_model`` is called from a background task:

- the update is folded in place: the store's replay merges this module's feed part (the
  surface's updates since its last checkpoint, held in memory) with the persisted parts;
- viewers get it as a transient ``a2ui.surface.upserted``, which stays out of the event
  bus's replay log;
- at most once per ``a2ui.live_checkpoint_s``, and when the task settles, the feed is
  appended to the transcript once, as a checkpoint: its messages from the last
  whole-model (``/``) update on, carrying the surface's revision as a floor so a later
  replay of the shorter checkpoint never moves the surface backwards. The feed part is
  dropped after its checkpoint is written, never before, so no fold misses an update.

A feed held in memory does not survive a restart, and neither does the task that fed it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

__all__ = ["checkpoint", "checkpoint_interval_s", "flush_task", "writer"]

_LOCK = threading.Lock()


def checkpoint_interval_s() -> float:
    """Seconds between two checkpoints of one fed surface (``a2ui.live_checkpoint_s``)."""
    from clio_agent import conf  # noqa: PLC0415

    value = conf.resolve(
        "a2ui.live_checkpoint_s", env="CLIO_A2UI_LIVE_CHECKPOINT_S", default=60.0, cast=float
    )
    return max(0.0, value)


def _feeds(app: Any) -> dict[str, dict[str, Any]]:
    feeds = getattr(app.state, "a2ui_live_feeds", None)
    if feeds is None:
        feeds = app.state.a2ui_live_feeds = {}
    return feeds


def _owners(app: Any) -> dict[tuple[str, str], dict[str, Any]]:
    owners = getattr(app.state, "a2ui_live_feed_owners", None)
    if owners is None:
        owners = app.state.a2ui_live_feed_owners = {}
    return owners


def writer(app: Any, session_id: str, handle: str) -> Callable[[Any], bool]:
    """The part writer for a background task's surface updates (never refuses)."""

    def write(part: Any) -> bool:
        surface_id = str(getattr(part, "surface_id", "") or "")
        with _LOCK:
            feeds = _feeds(app).setdefault(session_id, {})
            current = feeds.get(surface_id)
            messages = [
                *(current.a2ui_messages if current is not None else ()),
                *part.a2ui_messages,
            ]
            recorded = (current.metadata if current is not None else part.metadata)["recorded_at"]
            # A new id each update: the store's replay cache keys on part ids.
            feeds[surface_id] = part.model_copy(
                update={
                    "a2ui_messages": messages,
                    "metadata": {**part.metadata, "recorded_at": recorded},
                }
            )
            owner = _owners(app).setdefault(
                (session_id, surface_id), {"handle": handle, "last": time.monotonic()}
            )
            due = time.monotonic() - owner["last"] >= checkpoint_interval_s()
        if due:
            checkpoint(app, session_id, surface_id)
        return True

    return write


def _from_last_whole_model(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from clio_agent.gact.a2ui_component_fold import is_whole_model_update  # noqa: PLC0415

    starts = [i for i, message in enumerate(messages) if is_whole_model_update(message)]
    return messages[starts[-1] :] if starts else messages


def checkpoint(app: Any, session_id: str, surface_id: str) -> bool:
    """Append the surface's feed to the transcript once; whether there was one."""
    from clio_agent.gact.a2ui_component_fold import REVISION_FLOOR_KEY  # noqa: PLC0415

    with _LOCK:
        part = _feeds(app).get(session_id, {}).get(surface_id)
        owner = _owners(app).get((session_id, surface_id))
        if owner is not None:
            owner["last"] = time.monotonic()
    if part is None:
        return False
    record = app.state.a2ui_store.get(session_id, surface_id)
    revision = int(getattr(record, "revision", 0) or 0)
    persisted = part.model_copy(
        update={
            "id": "a2ui_" + uuid4().hex,
            "a2ui_messages": _from_last_whole_model(list(part.a2ui_messages)),
            "metadata": {**part.metadata, REVISION_FLOOR_KEY: revision, "live_checkpoint": True},
        }
    )
    app.state.a2ui_store._persist_part(session_id, persisted)
    with _LOCK:  # dropped after the checkpoint is written: no fold misses an update
        feeds = _feeds(app).get(session_id, {})
        if feeds.get(surface_id) is part:
            feeds.pop(surface_id, None)
    return True


def flush_task(app: Any, handle: str) -> None:
    """Checkpoint every surface a settling task fed, and forget it as their feeder."""
    with _LOCK:
        fed = [key for key, owner in _owners(app).items() if owner.get("handle") == handle]
    for session_id, surface_id in fed:
        checkpoint(app, session_id, surface_id)
        with _LOCK:
            _owners(app).pop((session_id, surface_id), None)
