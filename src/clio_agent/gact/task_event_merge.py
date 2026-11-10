"""A session's whole semantic log: the ``_events`` log with its task-event lane merged in.

A task event's one record is its lane (:mod:`clio_agent.gact.task_event_lane`); the
``_events`` log holds every other event. A reader of the whole log (the trace route,
the session export's ARC fallback) takes both through :func:`session_log_segments`.

Order: the two families are merged by ``occurred_at`` (the event's own wall time,
stored with it), each family keeping its own order on a tie. The process-wide logical
clock is not used here: after a restart it is recovered only past the chunks loaded
so far, so a lane append can carry a smaller tick than ``_events`` records written
before the restart. A reader that needs only task events reads the lane alone
(:func:`clio_agent.gact.task_event_lane.session_task_events`).
"""

from __future__ import annotations

import heapq
from collections.abc import Iterator
from typing import Any

from clio_agent.arc import lane_chunking

__all__ = ["session_log_segments"]


def _occurred(segment: Any) -> str:
    content = segment.content if isinstance(segment.content, dict) else {}
    return str(content.get("occurred_at") or "")


def _lane(arc: Any, session_id: str) -> Iterator[Any]:
    from clio_agent.gact.task_event_lane import TASK_EVENT_LANE  # noqa: PLC0415 - cycle

    for segment in lane_chunking.lane_segments(arc._segments, session_id, TASK_EVENT_LANE):
        if segment.kind == "semantic_event" and isinstance(segment.content, dict):
            yield segment


def session_log_segments(arc: Any, session_id: str) -> Iterator[Any]:
    """Yield ``session_id``'s semantic-event segments, ``_events`` and lane merged.

    Raises:
        AttributeError: ``arc`` has no live plane (the caller reports ARC unavailable).
    """
    log = arc._live.iter_session_event_segments(session_id)
    merged = heapq.merge(
        ((_occurred(s), 0, n, s) for n, s in enumerate(log)),
        ((_occurred(s), 1, n, s) for n, s in enumerate(_lane(arc, session_id))),
        key=lambda item: item[:3],
    )
    for item in merged:
        yield item[3]
