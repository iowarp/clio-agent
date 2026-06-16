"""Reconstruct the ARC live-context plane from the durable Trace.

The Trace is the immutable source of truth; ARC is a derived, mutable view. Every
applied context op is logged as an ``arc.op`` event carrying the FULL segment
dicts it wrote and the ids it tombstoned, so the live segment set is replayable
from those events alone — no prior ARC state required. This is what lets ARC be
compacted freely (the Trace never loses anything) and backs the trace-audit
acceptance test.

Layering: this module is ``arc/``-only and operates on plain event dicts (already
read from the trace), so it never imports ``gact/``.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

import msgspec

from clio_agent.arc.schema import Segment

logger = logging.getLogger(__name__)

# Must match clio_agent.gact.app.ARC_OP_EVENT_TYPE (kept as a literal to avoid an
# arc -> gact import).
ARC_OP_EVENT_TYPE = "arc.op"


def _segment_from_dict(data: dict[str, Any], *, trace_ref: str) -> Optional[Segment]:
    """Build a Segment from a logged ``segments_written`` dict, stamping its
    ``trace_ref`` to the replaying event's id (so a replayed segment is identical
    to the live one)."""
    try:
        seg = msgspec.convert(data, Segment)
    except (msgspec.ValidationError, TypeError) as exc:
        logger.warning("replay: skipping malformed segment dict: %s", exc)
        return None
    seg.trace_ref = trace_ref
    return seg


def reconstruct_arc_segments(
    events: Iterable[dict[str, Any]],
    *,
    scope_filter: Optional[str] = None,
    as_of_logical_time: Optional[int] = None,
) -> list[Segment]:
    """Replay ``arc.op`` events into the live segment set, in render order.

    Args:
        events: Full event dicts from the durable trace (any order; non-``arc.op``
            events are ignored). Each ``arc.op`` is self-describing.
        scope_filter: Keep only segments whose ``scope`` starts with this prefix
            (e.g. ``"agentX/"`` for agent-level, ``""``/``None`` for all).
        as_of_logical_time: Reconstruct the view as it was at this logical time —
            segments created at/before it and not tombstoned at/before it. ``None``
            yields the current live view.

    Returns:
        Live Segments sorted by ``(order, logical_time)`` — i.e. render order, so
        ``segments_to_keys`` over the result equals the live store's ``render_keys``.
    """
    ops = [e for e in events if e.get("event_type") == ARC_OP_EVENT_TYPE]
    # logical_time is the causal order; fall back to 0 for any malformed payload.
    ops.sort(key=lambda e: int((e.get("payload") or {}).get("logical_time") or 0))

    state: dict[str, Segment] = {}
    for ev in ops:
        payload = ev.get("payload") or {}
        event_id = str(ev.get("event_id", "") or "")
        op_lt = int(payload.get("logical_time") or 0)
        for sd in payload.get("segments_written") or []:
            seg = _segment_from_dict(sd, trace_ref=event_id)
            if seg is not None:
                state[seg.id] = seg
        for tid in payload.get("segments_tombstoned") or []:
            existing = state.get(str(tid))
            if existing is not None and existing.status == "live":
                existing.status = "tombstoned"
                existing.tombstoned_at = op_lt

    out: list[Segment] = []
    for seg in state.values():
        if scope_filter and not seg.scope.startswith(scope_filter):
            continue
        if as_of_logical_time is None:
            if seg.status == "live":
                out.append(seg)
        elif seg.logical_time <= as_of_logical_time and (
            seg.tombstoned_at == 0 or seg.tombstoned_at > as_of_logical_time
        ):
            out.append(seg)
    out.sort(key=lambda s: (s.order, s.logical_time))
    logger.debug(
        "replay: reconstructed %d live segments from %d arc.op events (scope=%s as_of=%s)",
        len(out), len(ops), scope_filter, as_of_logical_time,
    )
    return out
