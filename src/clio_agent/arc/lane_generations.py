"""Generations of a chunked lane: a whole-lane replace that is atomic.

A lane (the :mod:`clio_agent.arc.lane_chunking` grammar: chunk 1 is the bare ``base``,
chunk N is ``<base>/<N>``) is sometimes replaced whole -- the transcript lane on undo,
rewind, fork, compact, import and clear. Dropping the lane and re-appending it is not
atomic: a store failure halfway leaves a truncated lane, and when clio-core holds the
only copy there is nothing to repair it from. This module is the ONE replace:

1. the new content is written as a NEW generation -- its own chunk family under a
   fresh base -- while readers keep reading the current one;
2. the lane's generation pointer is switched to it in ONE record put;
3. the old generation is dropped.

A failure before step 2 leaves the current generation untouched and is raised typed
(:class:`LaneReplaceError`); readers always read the generation the pointer names
(:func:`current_base`), so they see the old lane or the new one, never a mix.

Grammar (under a lane base ``B``; none of these is a chunk of ``B`` -- ``is_chunk_of``
accepts only canonical decimal tails -- so no sibling or generation collides with
the family):

* generation 0 is ``B`` itself: a lane written before generations existed is
  generation 0 by construction (no migration);
* generation ``N >= 1`` is the chunk family based at ``B/g/N``;
* the pointer is the single-segment record ``B/gen`` holding ``{"generation": N}``;
  no record means generation 0.

Garbage bound: a replace that fails before its switch can leave a partial ``cur + 1``,
and one that fails after its switch can leave the old ``cur - 1``. Every replace first
erases both neighbours of the current generation, so at most generations
``cur - 1 .. cur + 1`` ever hold records, and nothing unreachable outlives the next
replace or erase. Erasing (:func:`erase_lane`) is a replace with nothing, then the
pointer's removal -- by then only empty generations remain, so no stale generation can
become current again.

The caller serializes writers: :func:`replace_lane` runs under the lane's own scope
lock (``store._lock_for(session_id, base)``, re-entrant), which every append of the
lane also takes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from clio_agent.arc.lane_chunking import drop_lane
from clio_agent.arc.schema import Segment, SegmentKind
from clio_agent.errors import ClioError
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.arc.segments import SegmentStore

__all__ = [
    "GENERATION_KIND",
    "LANE_GENERATION_SWITCHED",
    "LaneGenerationError",
    "LaneReplaceError",
    "current_base",
    "current_generation",
    "erase_lane",
    "generation_base",
    "pointer_scope",
    "replace_lane",
]

#: The segment kind of the pointer record (never content: ignored by every reader).
GENERATION_KIND: SegmentKind = "lane_generation"
#: Audited (debug-greppable) once a replace switched a lane to its new generation.
LANE_GENERATION_SWITCHED = "lane_generation_switched"

ReplaceStage = Literal["prepare", "write", "switch", "retire"]


class LaneGenerationError(ClioError):
    """The lane's generation pointer cannot be read as a generation number."""

    def __init__(self, session_id: str, base: str, problem: str) -> None:
        super().__init__(
            f"the generation pointer of lane {base!r} in session {session_id!r} is "
            f"unreadable ({problem})",
            error_type="lane_generation_unreadable",
            details={"session_id": session_id, "base": base, "problem": problem},
        )
        self.session_id = session_id
        self.base = base


class LaneReplaceError(ClioError):
    """A whole-lane replace did not complete.

    ``committed`` says which lane readers now see: ``False`` (stages ``prepare`` /
    ``write`` / ``switch``) -- the previous lane, untouched; ``True`` (stage
    ``retire``) -- the new lane; only the old generation's removal failed, and the
    next replace or erase of the lane removes it.
    """

    def __init__(
        self,
        session_id: str,
        base: str,
        stage: ReplaceStage,
        generation_from: int,
        generation_to: int,
        cause: BaseException,
    ) -> None:
        committed = stage == "retire"
        outcome = (
            "the new content is in place; the previous copy could not be removed yet"
            if committed
            else "the previous content is unchanged"
        )
        super().__init__(
            f"replacing lane {base!r} of session {session_id!r} failed at {stage} "
            f"({outcome}): {cause}",
            error_type="lane_replace_failed",
            details={
                "session_id": session_id,
                "base": base,
                "stage": stage,
                "committed": committed,
                "generation_from": generation_from,
                "generation_to": generation_to,
                "cause": type(cause).__name__,
            },
        )
        self.session_id = session_id
        self.stage = stage
        self.committed = committed


def pointer_scope(base: str) -> str:
    """The scope of ``base``'s generation pointer record."""

    return f"{base}/gen"


def generation_base(base: str, generation: int) -> str:
    """The chunk-family base of ``generation`` of lane ``base`` (``base`` itself for 0).

    Raises:
        ValueError: ``generation`` is negative.
    """

    if generation < 0:
        raise ValueError(f"a lane generation is never negative: {generation}")
    return base if generation == 0 else f"{base}/g/{generation}"


def current_generation(store: "SegmentStore", session_id: str, base: str) -> int:
    """The generation lane ``base`` currently reads from (0 when it has no pointer).

    One cached ``get`` of the pointer record (cheap once loaded, like every scope).

    Raises:
        LaneGenerationError: The pointer record exists but does not name a generation.
    """

    segs = store.list_segments(session_id, pointer_scope(base), include_tombstoned=False)
    if not segs:
        return 0
    if len(segs) != 1:
        raise LaneGenerationError(session_id, base, f"{len(segs)} pointer segments")
    value = segs[0].content.get("generation")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise LaneGenerationError(session_id, base, f"generation={value!r}")
    return value


def current_base(store: "SegmentStore", session_id: str, base: str) -> str:
    """The chunk-family base every reader and appender of lane ``base`` uses now."""

    return generation_base(base, current_generation(store, session_id, base))


def _write_pointer(store: "SegmentStore", session_id: str, base: str, generation: int) -> None:
    """Put the pointer record naming ``generation`` -- ONE store write.

    On a failed put the store discards its in-memory copy (the next read reloads the
    record clio-core still holds) and raises typed (``ArcPersistError``).
    """

    scope = pointer_scope(base)
    with store._lock_for(session_id, scope):
        segs = store._segs(session_id, scope)
        pointer = Segment(
            scope=scope,
            kind=GENERATION_KIND,
            content={"generation": generation},
            session_id=session_id,
            step=-1,
            order=1.0,
            logical_time=store._new_lt(),
        )
        store._index.drop_scope(session_id, scope)
        segs[:] = [pointer]
        store._index.add(session_id, scope, pointer)
        store._persist(session_id, scope)


def _erase_neighbours(store: "SegmentStore", session_id: str, base: str, current: int) -> None:
    """Drop the generations next to ``current`` (leftovers of a failed replace)."""

    for generation in (current - 1, current + 1):
        if generation >= 0:
            drop_lane(store, session_id, generation_base(base, generation))


def replace_lane(
    store: "SegmentStore",
    session_id: str,
    base: str,
    write: Callable[[str], Any],
) -> int:
    """Replace lane ``base`` whole, atomically for its readers.

    ``write(new_base)`` appends the new content into the new generation's chunk
    family (``new_base``); it must not touch the current one. Then the pointer is
    switched in one put and the old generation dropped.

    Args:
        store: The session's segment store.
        session_id: Owning session.
        base: The lane's base scope.
        write: Appends the new content under the base it is given.

    Returns:
        The new current generation.

    Raises:
        LaneReplaceError: A stage failed. Before the switch the lane is unchanged;
            at ``retire`` the new content is current (``committed``).
    """

    with store._lock_for(session_id, base):
        current = current_generation(store, session_id, base)
        target = current + 1
        new_base = generation_base(base, target)
        stage: ReplaceStage = "prepare"
        try:
            _erase_neighbours(store, session_id, base, current)
            stage = "write"
            write(new_base)
            stage = "switch"
            _write_pointer(store, session_id, base, target)
            stream_audit(
                LANE_GENERATION_SWITCHED,
                session_id=session_id,
                base=base,
                generation_from=current,
                generation_to=target,
            )
            stage = "retire"
            drop_lane(store, session_id, generation_base(base, current))
        except LaneReplaceError:
            raise
        except Exception as exc:  # re-raised typed: every stage names what readers see
            raise LaneReplaceError(session_id, base, stage, current, target, exc) from exc
        return target


def erase_lane(store: "SegmentStore", session_id: str, base: str) -> None:
    """Erase lane ``base`` whole: replace it with nothing, then remove the pointer.

    The replace leaves only empty generations, so removing the pointer (back to
    generation 0, itself empty) never makes stale content current.

    Raises:
        LaneReplaceError: The replace failed (see :func:`replace_lane`).
    """

    with store._lock_for(session_id, base):
        generation = replace_lane(store, session_id, base, lambda _new_base: None)
        try:
            store.drop_scope(session_id, pointer_scope(base))
        except Exception as exc:  # re-raised typed: the lane already reads as empty
            raise LaneReplaceError(
                session_id, base, "retire", generation - 1, generation, exc
            ) from exc
