"""Bounded live-view projection for relay application timeline events (#1131).

The authoritative run remains the session-backed ``AgentTask`` and durable relay
handle. This module keeps only bounded, process-local display rows keyed by that
task id; it is intentionally disposable and never participates in lifecycle folds.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

from fastapi.responses import StreamingResponse

from clio_agent.gact.agents.spawn_placement import run_handle_fields
from clio_agent.gact.events import Event
from clio_agent.gact.runtime.globals import _format_sse

if TYPE_CHECKING:
    from fastapi import FastAPI, Request

    from clio_agent.gact.agent_tasks import AgentTask

logger = logging.getLogger(__name__)

TIMELINE_ROW_EVENT = "timeline_row"
TIMELINE_DROP_EVENT = "timeline_drop"
TIMELINE_SCHEMA_VERSION = "clio.relay-timeline-row.v1"
TIMELINE_RING_MAX = 64
TIMELINE_DROP_MAX = 64

RELAY_TIMELINE_DROP_REASONS: dict[str, dict[str, Any]] = {
    "relay_timeline_malformed": {
        "category": "relay_timeline_contract",
        "description": "The relay timeline frame lacked a valid typed application-event shape.",
        "recovery_actions": ["continue_polling", "upgrade_relay"],
    },
    "relay_timeline_unknown_task": {
        "category": "relay_timeline_identity",
        "description": "The timeline frame named a task absent from the runs projection.",
        "recovery_actions": ["continue_polling", "refresh_runs"],
    },
    "relay_timeline_task_identity_mismatch": {
        "category": "relay_timeline_identity",
        "description": "The timeline frame named a different known task than its retained handle.",
        "recovery_actions": ["continue_polling", "reconnect_task_stream"],
    },
    "relay_timeline_unroutable": {
        "category": "relay_timeline_contract",
        "description": "The non-lifecycle relay event matched no supported live-view producer.",
        "recovery_actions": ["continue_polling", "upgrade_relay"],
    },
}

_APPLICATION_PREFIXES = ("jarvis.", "mcp_call.")
_APPLICATION_EVENT_TYPES = frozenset({"progress", "log", "metric", "artifact", "status"})
_APPLICATION_SOURCES = frozenset({"jarvis", "mcp_call"})
_INSTALL_LOCK = threading.Lock()


class RelayTimelineProjection:
    """Thread-safe bounded display rings for relay rows and typed drops."""

    def __init__(
        self, *, max_rows: int = TIMELINE_RING_MAX, max_drops: int = TIMELINE_DROP_MAX
    ) -> None:
        if max_rows < 1 or max_drops < 1:
            raise ValueError("relay timeline bounds must be positive")
        self._max_rows = max_rows
        self._max_drops = max_drops
        self._lock = threading.RLock()
        self._rows: dict[str, deque[Event]] = {}
        self._drops: dict[str, deque[dict[str, Any]]] = {}

    def append_row(self, task_id: str, event: Event) -> None:
        """Append one row event, evicting the oldest row at the configured bound."""

        with self._lock:
            self._rows.setdefault(task_id, deque(maxlen=self._max_rows)).append(event)

    def append_drop(self, task_id: str, drop: dict[str, Any]) -> None:
        """Record one structured routing drop at the configured bound."""

        with self._lock:
            self._drops.setdefault(task_id, deque(maxlen=self._max_drops)).append(dict(drop))

    def row_events(self, task_id: str) -> list[Event]:
        """Return the task's retained row events oldest first."""

        with self._lock:
            return list(self._rows.get(task_id, ()))

    def rows(self, task_id: str) -> list[dict[str, Any]]:
        """Return wire row payloads for a JSON live-handle projection."""

        return [dict(event.payload) for event in self.row_events(task_id)]

    def drops(self, task_id: str) -> list[dict[str, Any]]:
        """Return recorded typed drops oldest first."""

        with self._lock:
            return [dict(drop) for drop in self._drops.get(task_id, ())]


def relay_timeline_projection(
    app: "FastAPI", *, create: bool = True
) -> RelayTimelineProjection | None:
    """Resolve the app's disposable display projection, optionally installing it."""

    projection = getattr(app.state, "relay_timeline_projection", None)
    if isinstance(projection, RelayTimelineProjection) or not create:
        return projection
    with _INSTALL_LOCK:
        projection = getattr(app.state, "relay_timeline_projection", None)
        if not isinstance(projection, RelayTimelineProjection):
            projection = RelayTimelineProjection()
            app.state.relay_timeline_projection = projection
    return projection


def relay_timeline_view(
    app: "FastAPI", task_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read the task's retained rows and drops without creating display state."""

    projection = relay_timeline_projection(app, create=False)
    if projection is None:
        return [], []
    return projection.rows(task_id), projection.drops(task_id)


def _is_application_event(raw: Mapping[str, Any], event_type: str) -> bool:
    """Return whether a non-lifecycle event belongs to a supported application lane."""

    source = str(raw.get("source") or raw.get("kind") or "")
    return (
        event_type in _APPLICATION_EVENT_TYPES
        or event_type.startswith(_APPLICATION_PREFIXES)
        or source in _APPLICATION_SOURCES
    )


def record_relay_timeline_drop(
    app: "FastAPI",
    handle: Any,
    reason: str,
    *,
    raw: Any = None,
    message: str = "",
) -> dict[str, Any]:
    """Record and, when possible, publish one catalogued non-silent drop."""

    definition = RELAY_TIMELINE_DROP_REASONS.get(reason)
    if definition is None:
        raise ValueError(f"unknown relay timeline drop reason: {reason}")
    expected_task_id = str(getattr(handle, "task_id", "") or "")
    observed_task_id = str(raw.get("task_id") or "") if isinstance(raw, Mapping) else ""
    event_type = str(raw.get("event_type") or "") if isinstance(raw, Mapping) else ""
    drop = {
        "reason": reason,
        **{
            key: list(value) if isinstance(value, list) else value
            for key, value in definition.items()
        },
        "task_id": expected_task_id,
        "observed_task_id": observed_task_id,
        "event_type": event_type,
        "message": message,
    }
    projection = relay_timeline_projection(app)
    assert projection is not None
    projection.append_drop(expected_task_id, drop)
    logger.warning(
        "relay timeline drop reason=%s task=%s observed_task=%s event_type=%s message=%s",
        reason,
        expected_task_id,
        observed_task_id,
        event_type,
        message,
    )
    task = app.state.agent_task_registry.get(expected_task_id)
    if task is not None and task.child_session_id:
        app.state.bus.publish(
            Event(type=TIMELINE_DROP_EVENT, session_id=task.child_session_id, payload=drop)
        )
    return drop


def route_relay_timeline_event(app: "FastAPI", handle: Any, raw: Any) -> bool:
    """Validate and route one non-``agent.task.*`` event to the task display ring.

    Every refusal records a catalogued drop. A successful row is appended before
    publication so a simultaneous live-door connect can replay it from the ring.
    """

    expected_task_id = str(getattr(handle, "task_id", "") or "")
    expected = app.state.agent_task_registry.get(expected_task_id)
    if expected is None:
        record_relay_timeline_drop(
            app,
            handle,
            "relay_timeline_unknown_task",
            raw=raw,
            message="retained handle is absent from AgentTaskRegistry",
        )
        return False
    if not isinstance(raw, Mapping):
        record_relay_timeline_drop(
            app, handle, "relay_timeline_malformed", raw=raw, message="frame is not a mapping"
        )
        return False
    observed_task_id = raw.get("task_id")
    if not isinstance(observed_task_id, str) or not observed_task_id:
        record_relay_timeline_drop(
            app, handle, "relay_timeline_malformed", raw=raw, message="task_id is missing"
        )
        return False
    if observed_task_id != expected_task_id:
        reason = (
            "relay_timeline_unknown_task"
            if app.state.agent_task_registry.get(observed_task_id) is None
            else "relay_timeline_task_identity_mismatch"
        )
        record_relay_timeline_drop(
            app, handle, reason, raw=raw, message="frame task_id disagrees with retained handle"
        )
        return False
    event_type_obj = raw.get("event_type")
    if not isinstance(event_type_obj, str) or not event_type_obj.strip():
        record_relay_timeline_drop(
            app, handle, "relay_timeline_malformed", raw=raw, message="event_type is missing"
        )
        return False
    event_type = event_type_obj.strip()
    sequence_obj = raw.get("seq", 0)
    if isinstance(sequence_obj, bool) or not isinstance(sequence_obj, int) or sequence_obj < 0:
        record_relay_timeline_drop(
            app,
            handle,
            "relay_timeline_malformed",
            raw=raw,
            message="seq must be a non-negative int",
        )
        return False
    payload_obj = raw.get("payload", {})
    if not isinstance(payload_obj, Mapping):
        record_relay_timeline_drop(
            app, handle, "relay_timeline_malformed", raw=raw, message="payload must be a mapping"
        )
        return False
    if event_type.startswith("agent.task.") or not _is_application_event(raw, event_type):
        record_relay_timeline_drop(
            app,
            handle,
            "relay_timeline_unroutable",
            raw=raw,
            message="event matched neither lifecycle fold nor application timeline",
        )
        return False

    child_id = expected.agent_ref.get("expert_id", "")
    fields = run_handle_fields(expected, child_id)
    row = {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "task_id": expected_task_id,
        "handle_id": fields["handle_id"],
        "run_label": fields["run_label"],
        "live_state": fields["live_state"],
        "host": fields["host"],
        "placement": fields["placement"],
        "sequence": sequence_obj,
        "event_type": event_type,
        "source": str(raw.get("source") or raw.get("kind") or ""),
        "summary": str(raw.get("summary") or ""),
        "occurred_at": str(raw.get("occurred_at") or raw.get("created_at") or ""),
        "payload": dict(payload_obj),
    }
    event = Event(type=TIMELINE_ROW_EVENT, session_id=expected.child_session_id, payload=row)
    projection = relay_timeline_projection(app)
    assert projection is not None
    projection.append_row(expected_task_id, event)
    app.state.bus.publish(event)
    return True


def relay_timeline_stream_response(
    app: "FastAPI", task: "AgentTask", request: "Request"
) -> StreamingResponse:
    """Return the same ``/live`` door as an SSE row stream via content negotiation."""

    try:
        last_event_id = int(request.headers.get("last-event-id", "0"))
    except (TypeError, ValueError):
        last_event_id = 0

    async def event_stream() -> AsyncIterator[bytes]:
        projection = relay_timeline_projection(app, create=False)
        retained = projection.row_events(task.task_id) if projection is not None else []
        replayed_max = last_event_id
        for event in retained:
            if event.id <= last_event_id:
                continue
            yield _format_sse(event.replay_copy())
            replayed_max = max(replayed_max, event.id)
        subscription = app.state.bus.subscribe(
            task.child_session_id,
            last_event_id=replayed_max,
        )
        async for event in subscription:
            if event.type not in {TIMELINE_ROW_EVENT, TIMELINE_DROP_EVENT}:
                continue
            if str(event.payload.get("task_id") or "") != task.task_id:
                continue
            yield _format_sse(event)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
