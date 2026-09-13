"""Which store records can carry a ``.text`` search companion (#1334 RPC diet).

``ClioCoreStore.put`` keeps a plain-text companion next to a record for BM25 scope
search and, when a put carries no text, probes the daemon (one native ``GetBlobSize``)
for a stale companion to drop. The reserved ``_events`` scope family (the semantic-event
log, the message-part atoms ``_events/m``, the working-set content lanes ``_events/w/*``)
is search-excluded by construction (``working_set_fold._default_search_indexed``), so a
``segments`` record under it can NEVER have a companion and the probe there is pure
overhead: every such put paid two native RPCs for one write. This policy lets the store
skip the probe for exactly that family and nothing else.

The record name is ``<session_id>__<scope with '/' -> '~'>``
(``segments.SegmentStore._record_name``); the prefix is ``arc.live.EVENTS_SCOPE`` (kept
literal here so ``arc.storage`` stays a leaf, pinned by a test).
"""

from __future__ import annotations

NEVER_INDEXED_SCOPE_PREFIX = "_events"
SEGMENT_NAME_SEP = "__"


def may_carry_companion(kind: str, name: str) -> bool:
    """Whether a ``(kind, name)`` record can ever have a ``.text`` companion.

    Args:
        kind: The record kind (``"segments"`` is the only kind with scoped names).
        name: The record stem.

    Returns:
        ``False`` only for a ``segments`` record whose scope is under ``_events``.
    """

    if kind != "segments":
        return True
    _sid, sep, scope = name.partition(SEGMENT_NAME_SEP)
    return not (sep and scope.startswith(NEVER_INDEXED_SCOPE_PREFIX))


__all__ = ["NEVER_INDEXED_SCOPE_PREFIX", "SEGMENT_NAME_SEP", "may_carry_companion"]
