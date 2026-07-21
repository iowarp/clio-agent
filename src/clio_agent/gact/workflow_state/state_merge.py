"""workflow_state as the recorded RESULT of a ``state_merge`` op (#737 S6).

The unified-ARC highway (``docs/design/unified-arc-highway.md`` §2.5, §2.8.d, §4.2
step 6) makes a delegated turn's ``workflow_state`` the *recorded result* of a
``state_merge`` operation — **never re-folded on read**.

The reason this slice exists (design §2.8.d): the merge
:func:`clio_agent.gact.workflow_state.merge._merge_workflow_state_mapping` is
parameterized by the **live pack's** ``WorkflowStateSchema`` (``rank`` /
``normalize_section`` / ``sticky_true_fields_for``) and is *order-sensitive*. So
re-folding the same handoff-row inputs under a *newer* pack schema yields a
*different* dict — which would silently break the frozen §4.5 / surface-1.9
``workflow_state`` bytes on a reload. Per the owner's action->result ruling ("a
compress event records what was folded and what it produced"), the merge **records
its RESULT** as a ``state_merge`` op carrying ``{inputs, produced, schema_version}``;
re-derivation replays to the recorded ``produced``, and ``schema_version`` pins the
semantics. The projection materializes that recorded result onto the message and
both delegate rows — a **schema-free** lookup that touches no ``normalize_section``.

Design decisions (each answering a named constraint):

* **Rides the S4/S5 atom lane, raw (§2.9, §2.10).** The op is a new additive
  :data:`~clio_agent.arc.schema.SegmentKind` (``state_merge``) on a dedicated
  ``_events/s`` partition — a sibling of the S4 ``_events/m`` message-part lane and
  the S2 ``_events/w`` working-set lane. Under ``_events`` it is search-excluded and
  lifecycle-erased with the log; being neither a working-set kind nor
  ``semantic_event`` it never reaches a prompt or a render. It is appended through the
  S4 raw primitive :func:`~clio_agent.gact.part_atoms._append_segment_raw`, which
  NEVER invokes ``_finish_write`` / the ``op_logger`` (routing a log write back
  through the op-logger re-forms the documented ``record -> op_logger -> arc.op ->
  record`` recursion, §2.9).

* **Recorded at the persist seam; ``produced`` is the merge site's OWN result.** The
  merge already happened upstream (the spawn runtime / ``turn_finalize``): each
  ``message.metadata["expert_handoffs"]`` row carries the merged ``workflow_state``
  the merge site produced. This slice does NOT re-run the merge — it CAPTURES that
  already-produced result as ``produced``, and captures the RAW carriers still visible
  on the row (``tools_called[].workflow_state`` + ``children[].workflow_state``) as
  ``inputs`` provenance. So a re-fold of ``inputs`` under a mutated schema would
  diverge from the stored ``produced`` — which is exactly the divergence the read-path
  refuses to author (the sabotage-a contrast).

* **Schema-free read (design (a)/(c)).** :func:`materialize_state_merge_projection`
  re-attaches ``produced`` onto each delegate row by a per-row scope key. It calls no
  schema method — no ``rank`` / ``normalize_section`` / ``sticky_true_fields_for`` — so
  the served bytes are frozen at write regardless of the pack schema live at read time.

* **Session-scoped flag = the S5 regime pin (§4.4b/c).** The op write AND the read
  materialization both ride the **atoms** regime pinned by
  :mod:`clio_agent.gact.transcript_projection` (single regime since v0.8.0): under
  the legacy regime the transcript is served from the messages-store ledger (which
  already carries ``workflow_state`` verbatim and is never re-folded), so recording or
  materializing a ``state_merge`` op there would be dead weight. A separate flag would
  admit a nonsensical "record ops but read legacy" split-brain. So there is **no S6
  flag of its own** — it is meaningful only in the atoms regime, and ships OFF by
  default exactly like S2/S4/S5 (design §5.2 Q4 honesty).

* **No silent fallback, best-effort-but-loud (§3.4).** The op is a *secondary*
  authoritative record: the same ``produced`` bytes also live verbatim in the S4
  message-part atom envelope (``metadata.expert_handoffs[].workflow_state``), so a
  failed op-record is NOT permanent loss — the read path degrades to the verbatim value
  (also frozen-at-write, also schema-free). A failed record therefore raises no turn
  failure but emits a typed :data:`STATE_MERGE_RECORD_FAILED_REASON` on the log, never a
  bare swallow.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from clio_agent.arc.live import EVENTS_SCOPE
from clio_agent.arc.schema import Segment, SegmentKind
from clio_agent.gact.part_atoms import _append_segment_raw
from clio_agent.gact.types import Message

logger = logging.getLogger(__name__)

# The op kind (a new, additive ``SegmentKind`` member) and the reserved content lane:
# a partition UNDER ``_events`` (so ``is_events_scope`` is True — search-excluded +
# lifecycle-erased with the log) distinct from the bare ``_events``/``_events/N``
# semantic-event chunks, the S2 fold's ``_events/w`` content lane, and the S4
# ``_events/m`` message-part lane.
STATE_MERGE_KIND: SegmentKind = "state_merge"
STATE_MERGE_SCOPE = f"{EVENTS_SCOPE}/s"

# Bumped only on a breaking change to the merge SEMANTICS (the ``WorkflowStateSchema``
# rank/normalize/sticky contract) — the pin the design §2.8.d requires so a recorded
# result is understood under the semantics that produced it, never re-folded under a
# newer one. Stored on every op record.
STATE_MERGE_SCHEMA_VERSION = 1

# The typed no-silent-fallback reason (the ``stream_fallback`` catalog style, §3.4).
STATE_MERGE_RECORD_FAILED_REASON = "state_merge_record_failed"

# The message-metadata key the delegate rows live under (SPEC §4.5 / surface 1.9).
_EXPERT_HANDOFFS_KEY = "expert_handoffs"


# --------------------------------------------------------------------------- #
# Pure entry construction (§2.5 action->result: inputs + produced + schema_version)
# --------------------------------------------------------------------------- #


def delegation_scope_key(message_id: str, row_index: int, row: Mapping[str, Any]) -> str:
    """Return a stable per-delegation scope key for one handoff row.

    The key must be reconstructable byte-identically from the ASSEMBLED (verbatim)
    message on read — the assembled ``expert_handoffs`` rows are byte-equal and in the
    same order as at write, so ``(message_id, row_index)`` is stable; the child/parent/
    stage suffix makes the key legible in the trace without changing its stability.

    Args:
        message_id: The owning message id.
        row_index: The row's 0-based position in ``metadata["expert_handoffs"]``.
        row: The handoff row.

    Returns:
        The scope key (e.g. ``"msg_1#2:data<-main@delegate.completed"``).
    """

    child = str(row.get("agent_id") or "")
    parent = str(row.get("parent_id") or "")
    stage = str(row.get("stage") or "")
    return f"{message_id}#{row_index}:{child}<-{parent}@{stage}"


def _raw_workflow_state_inputs(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Collect the RAW ``workflow_state`` carriers a re-fold of this row would consume.

    These are the un-merged mappings the upstream merge folded into the row's final
    ``workflow_state``: the row's ``tools_called[].workflow_state`` and, recursively,
    its ``children[].workflow_state`` (and their nested carriers). Stored as ``inputs``
    provenance so the op is a full action->result record — and so a read that RE-FOLDED
    them under a mutated schema would diverge from the stored ``produced`` (the divergence
    the read path refuses to author).

    Args:
        row: The handoff row.

    Returns:
        The raw carrier mappings, in discovery order (may be empty).
    """

    inputs: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if not isinstance(node, Mapping):
            return
        carrier = node.get("workflow_state")
        if isinstance(carrier, Mapping) and carrier:
            inputs.append({str(k): v for k, v in carrier.items()})
        for call in node.get("tools_called") or []:
            if isinstance(call, Mapping):
                call_state = call.get("workflow_state")
                if isinstance(call_state, Mapping) and call_state:
                    inputs.append({str(k): v for k, v in call_state.items()})
        for child in node.get("children") or []:
            visit(child)

    for call in row.get("tools_called") or []:
        if isinstance(call, Mapping):
            call_state = call.get("workflow_state")
            if isinstance(call_state, Mapping) and call_state:
                inputs.append({str(k): v for k, v in call_state.items()})
    for child in row.get("children") or []:
        visit(child)
    return inputs


def build_state_merge_entries(message: Message) -> list[dict[str, Any]]:
    """Build the per-delegation ``state_merge`` entries for one message (pure).

    One entry per ``metadata["expert_handoffs"]`` row that carries a non-empty typed
    ``workflow_state`` (the RESULT the upstream merge produced). Each entry is
    ``{scope, inputs, produced}`` — ``produced`` captured verbatim (never re-merged),
    ``inputs`` the raw carriers for provenance/replay. A message with no delegated
    workflow_state yields ``[]`` (no op is recorded).

    Args:
        message: The persisted gact message.

    Returns:
        The list of entry dicts, in row order.
    """

    rows = (message.metadata or {}).get(_EXPERT_HANDOFFS_KEY)
    if not isinstance(rows, list):
        return []
    entries: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            continue
        produced = row.get("workflow_state")
        if not isinstance(produced, Mapping) or not produced:
            continue
        entries.append(
            {
                "scope": delegation_scope_key(message.id, index, row),
                "inputs": _raw_workflow_state_inputs(row),
                "produced": {str(k): v for k, v in produced.items()},
            }
        )
    return entries


def build_state_merge_content(message: Message) -> dict[str, Any] | None:
    """Build one ``state_merge`` atom ``content`` for a message, or ``None`` if empty.

    Args:
        message: The persisted gact message.

    Returns:
        The atom ``content`` (``{schema_version, op, message_id, entries}``) when the
        message carries at least one delegated ``workflow_state``, else ``None``.
    """

    entries = build_state_merge_entries(message)
    if not entries:
        return None
    return {
        "schema_version": STATE_MERGE_SCHEMA_VERSION,
        "op": "state_merge",
        "message_id": message.id,
        "entries": entries,
    }


# --------------------------------------------------------------------------- #
# The raw log append (§2.9) — reuses the S4 raw primitive (never the op_logger)
# --------------------------------------------------------------------------- #


def record_state_merge(arc: Any, session_id: str, message: Message) -> Segment | None:
    """Record a message's ``state_merge`` op onto the canonical ``_events/s`` lane.

    Appends one atom carrying every delegation's ``{inputs, produced, schema_version}``
    for the message (design §2.5). A message with no delegated ``workflow_state`` records
    nothing (returns ``None``). Uses the S4 raw append (§2.9) — never the op-logger.

    Args:
        arc: The process ARC memory (``ARCMemory``); its ``_segments`` store is used.
        session_id: Owning session.
        message: The just-persisted gact message.

    Returns:
        The appended segment, or ``None`` when there was no op to record.
    """

    content = build_state_merge_content(message)
    if content is None:
        return None
    store = arc._segments
    return _append_segment_raw(store, session_id, STATE_MERGE_SCOPE, STATE_MERGE_KIND, content)


def record_state_merge_best_effort(arc: Any, session_id: str, message: Message) -> None:
    """Record a ``state_merge`` op, downgrading a failure to a typed loud reason (§3.4).

    The op is a SECONDARY record — the same ``produced`` bytes also live verbatim in the
    S4 message-part atom, so a failed record degrades the read to that verbatim value
    (also frozen-at-write, also schema-free), never permanent loss. So a failure emits
    :data:`STATE_MERGE_RECORD_FAILED_REASON` and continues; it never fails the turn.

    Args:
        arc: The process ARC memory.
        session_id: Owning session.
        message: The just-persisted gact message.
    """

    try:
        record_state_merge(arc, session_id, message)
    except Exception:  # noqa: BLE001 - downgraded to a typed, loud, non-fatal reason
        logger.error(
            "state_merge: record FAILED reason=%s session=%s message=%s "
            "(verbatim message-part copy is the frozen fallback; read stays schema-free)",
            STATE_MERGE_RECORD_FAILED_REASON,
            session_id,
            getattr(message, "id", ""),
            exc_info=True,
        )


def drop_state_merge_lane(arc: Any, session_id: str) -> None:
    """Drop a session's ``_events/s`` op lane (transcript-projection erasure).

    Mirrors the S5 ``_events/m`` drop on undo/rewind/``DELETE``/fork/compact/import: the
    ops are re-materialisations of the gact-visible transcript ONLY, so dropping them
    touches no ARC working-set scope (``gact_visible_transcript_only`` holds). A cheap
    partition drop when no ops exist.

    Args:
        arc: The process ARC memory.
        session_id: Owning session.
    """

    arc._segments.drop_scope(session_id, STATE_MERGE_SCOPE)


# --------------------------------------------------------------------------- #
# Read-path (§2.8.d) — the recorded RESULT, materialized schema-free (NO re-fold)
# --------------------------------------------------------------------------- #


def load_state_merge_results(arc: Any, session_id: str) -> dict[str, dict[str, Any]]:
    """Read a session's recorded ``state_merge`` results, keyed by delegation scope.

    Reads the ``_events/s`` lane in append order and returns ``{scope: produced}`` with
    the LAST op per scope winning (so a re-materialisation after undo/rewind/compact
    supersedes an earlier record). This is the ONLY read of the merge result — no caller
    re-runs the fold.

    Args:
        arc: The process ARC memory.
        session_id: Owning session.

    Returns:
        The per-delegation recorded ``produced`` mappings (empty when the session has
        no ``state_merge`` op).
    """

    store = arc._segments
    results: dict[str, dict[str, Any]] = {}
    for seg in store.list_segments(session_id, STATE_MERGE_SCOPE, include_tombstoned=False):
        content = seg.content
        for entry in content.get("entries") or []:
            if not isinstance(entry, Mapping):
                continue
            scope = str(entry.get("scope") or "")
            produced = entry.get("produced")
            if scope and isinstance(produced, Mapping):
                results[scope] = {str(k): v for k, v in produced.items()}
    return results


def resolve_row_workflow_state(
    recorded: Mapping[str, dict[str, Any]],
    message_id: str,
    row_index: int,
    row: Mapping[str, Any],
) -> Any:
    """Return the served ``workflow_state`` for one delegate row — the recorded RESULT.

    The read-path invariant (design (c)): the served value is the recorded ``produced``
    for the row's scope when present, else the verbatim value already on the row (also
    frozen-at-write, also schema-free — the graceful fallback when no op was recorded,
    e.g. a pre-S6 ledger). **No schema method is called** — the served bytes are frozen
    at write time and identical under any pack schema live at read (the sabotage-a
    invariant: swapping this for a ``_merge_workflow_state_mapping`` re-fold under the
    current schema would diverge and turn the replay test red).

    Args:
        recorded: The session's ``{scope: produced}`` map (:func:`load_state_merge_results`).
        message_id: The owning message id.
        row_index: The row's 0-based position in ``metadata["expert_handoffs"]``.
        row: The handoff row.

    Returns:
        The recorded ``produced`` mapping, or the row's own ``workflow_state`` verbatim.
    """

    key = delegation_scope_key(message_id, row_index, row)
    produced = recorded.get(key)
    if produced is not None:
        return produced
    return row.get("workflow_state")


def materialize_state_merge_projection(arc: Any, session_id: str, messages: list[Message]) -> None:
    """Materialize the recorded ``state_merge`` results onto an assembled transcript.

    The design (a) projection: ``workflow_state`` = the recorded RESULT of the last
    ``state_merge`` op for the scope, materialized onto the message and both delegate
    rows. Mutates each message's ``metadata["expert_handoffs"]`` rows in place, routing
    every row's ``workflow_state`` through :func:`resolve_row_workflow_state` — the
    single schema-free read seam. A no-op when the session recorded no op AND every row
    keeps its verbatim value (the resolver returns the same bytes).

    Args:
        arc: The process ARC memory.
        session_id: Owning session.
        messages: The assembled transcript (mutated in place).
    """

    recorded = load_state_merge_results(arc, session_id)
    if not recorded:
        return  # no op recorded: rows keep their verbatim (also frozen, schema-free) value
    for message in messages:
        rows = (message.metadata or {}).get(_EXPERT_HANDOFFS_KEY)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            if not isinstance(row.get("workflow_state"), Mapping):
                continue
            row["workflow_state"] = resolve_row_workflow_state(recorded, message.id, index, row)


__all__ = [
    "STATE_MERGE_KIND",
    "STATE_MERGE_RECORD_FAILED_REASON",
    "STATE_MERGE_SCHEMA_VERSION",
    "STATE_MERGE_SCOPE",
    "build_state_merge_content",
    "build_state_merge_entries",
    "delegation_scope_key",
    "drop_state_merge_lane",
    "load_state_merge_results",
    "materialize_state_merge_projection",
    "record_state_merge",
    "record_state_merge_best_effort",
    "resolve_row_workflow_state",
]
