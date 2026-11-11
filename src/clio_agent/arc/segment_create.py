"""Content-addressed segment records, created once (iowarp-core 2.4 put-if-absent).

A record whose name is derived from its content (task content: ``_events/t/content/<sha>``)
is either absent or already holds those bytes. It used to cost a probe (a read of the
record) and then a put, and two writers could both see it absent. :func:`create_scope_once`
puts the record's first chunk with ``put_if_absent`` (``AsyncPutBlobIfAbsent``): one round
trip, and clio-core decides which writer creates it.

The probe-then-append path stays, with a typed reason logged, where the single create
does not fit:

* inside a :func:`~clio_agent.arc.segment_group.segment_group` the group already probes
  every scope it creates with one ``sizes`` call and puts them with one ``put_many`` per
  rank; a create per record would cost one round trip each (``segment_group``);
* a scope logged as an ``arc.op`` must go through ``append`` so its op is recorded
  (``context_op_scope``);
* a store without ``put_if_absent`` (``store_without_put_if_absent``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from clio_agent.arc.loop_guard import LoopThreadStoreWrite, assert_store_write_off_loop
from clio_agent.arc.replay import logs_context_op
from clio_agent.arc.schema import Segment, SegmentKind
from clio_agent.arc.segment_errors import ArcPersistError
from clio_agent.arc.segment_group import active_group

if TYPE_CHECKING:
    from clio_agent.arc.segments import SegmentStore

logger = logging.getLogger(__name__)

#: Reason logged when a create falls back to probe-then-append.
CREATE_PROBED = "segment_create_probed"

__all__ = ["CREATE_PROBED", "create_scope_once"]


def _probe_reason(store: SegmentStore, scope: str) -> str:
    """Why this create cannot be one ``put_if_absent``, or ``""`` when it can."""
    if active_group(store) is not None:
        return "segment_group"
    if logs_context_op(scope):
        return "context_op_scope"
    if not callable(getattr(store._store, "put_if_absent", None)):
        return "store_without_put_if_absent"
    return ""


def create_scope_once(
    store: SegmentStore,
    session_id: str,
    scope: str,
    kind: SegmentKind,
    content: dict[str, Any],
    *,
    turn_id: str = "",
) -> bool:
    """Create ``scope`` holding one segment of ``content`` unless it already exists.

    Returns ``True`` when this call created the record, ``False`` when it was held
    already (cached here, or stored by an earlier process or another writer; the
    caller verifies it by reading). A record this process holds costs no round trip.

    Raises:
        ArcPersistError: clio-core refused the create; nothing is cached.
        LoopThreadStoreWrite: called from the event loop thread.
    """
    from clio_agent.arc.segments import _coerce_content  # noqa: PLC0415 - import cycle

    key = (session_id, scope)
    with store._lock_for(session_id, scope):
        if key in store._loaded and store._scopes.get(key):
            return False
        reason = _probe_reason(store, scope)
        if reason:
            logger.debug(
                "reason=%s cause=%s session=%s scope=%s", CREATE_PROBED, reason, session_id, scope
            )
            if store.list_segments(session_id, scope):
                return False
            store.append(session_id, scope, kind, content, turn_id=turn_id)
            return True
        seg = Segment(
            scope=scope,
            kind=kind,
            content=_coerce_content(content),
            session_id=session_id,
            step=-1,
            order=1.0,
            logical_time=store._new_lt(),
            turn_id=turn_id,
        )
        assert_store_write_off_loop("segments.put", scope=scope)
        name, write, search_text = store._planned_put(session_id, scope, [seg], None)
        try:
            created = store._store.put_if_absent(
                "segments", name, write.data, search_text=search_text
            )
        except LoopThreadStoreWrite:
            raise  # a write from the event loop is a caller bug, typed as itself
        except Exception as exc:  # noqa: BLE001 - re-raised typed; nothing was cached
            raise ArcPersistError(session_id, scope, exc) from exc
        if not created:
            store._discard_scope(session_id, scope)  # the stored record is read on demand
            return False
        store._install_committed(session_id, scope, [seg], write.committed([seg]))
        store._index.add(session_id, scope, seg)
        return True
