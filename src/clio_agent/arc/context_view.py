"""The context view: one (session, logical scope)'s folded live context, kept current.

The working set is a fold of the canonical ``_events/w`` content lane
(:mod:`clio_agent.arc.working_set_fold`). Re-folding the whole lane on every read and
every append made both cost O(history). A :class:`ContextView` holds the fold's result
for one logical scope and keeps it current:

* an **append** adds its atom at the end of the live list (append order is
  ``max(order) + 1``, so it always renders last) -- O(1), no fold;
* a recorded **op** (delete, replace, summarize, insert) rebuilds the view by folding
  the atoms it holds -- the ones at or after the scope's **anchor**;
* a **summarize** that retires every earlier atom of the scope moves the anchor to its
  summary atom: nothing before it can ever be live again (retirement is permanent in
  the append-only log), so the view drops those atoms and a cold read after a restart
  starts from the anchor (snapshot + tail, ``unified-arc-highway.md`` §2.10).

There is ONE fold implementation, :func:`fold_atoms`. The view folds the atoms at or
after its anchor with it; ``as_of`` and tombstone (history) reads fold the scope's
whole log with it. Order allocation is :func:`order_at`, shared with the plain
:class:`~clio_agent.arc.segments.SegmentStore`.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import msgspec

from clio_agent.arc.schema import Segment, SegmentKind

__all__ = [
    "NON_CONTENT_KINDS",
    "STEP_OPEN_KIND",
    "WS_OP_KIND",
    "AnchorEntry",
    "ContextView",
    "ViewSnapshot",
    "fold_atoms",
    "next_generation",
    "order_at",
    "retirements",
]

# Log-internal atom kinds that are NEVER renderable content: the append-only op
# record and the pre-execution crash breadcrumb. Excluded from every fold render.
WS_OP_KIND: SegmentKind = "ws_op"
STEP_OPEN_KIND: SegmentKind = "step_open"
NON_CONTENT_KINDS = frozenset({WS_OP_KIND, STEP_OPEN_KIND})


# Process-wide so a generation is never reused: a view rebuilt after a release (or the
# plain store after an op) can never repeat a generation a reader cached earlier.
_GENERATIONS = itertools.count(1)


def next_generation() -> int:
    """A generation number never handed out before in this process."""
    return next(_GENERATIONS)


class AnchorEntry(msgspec.Struct):
    """Where a scope's live context starts: a summary atom that retired all before it.

    Attributes:
        atom_id: The summary atom's id.
        logical_time: Its clock; every content atom of the scope created before it is
            retired, so the view and a cold read only need atoms at or after it.
        chunk: The content-lane chunk scope the summary atom lives in.
        min_order: The lowest ``order`` over every content atom of the scope up to the
            anchor (``insert`` at position 0 allocates below it).
        max_order: The highest such ``order`` (``append`` allocates above it).
    """

    atom_id: str
    logical_time: int
    chunk: str
    min_order: float
    max_order: float


@dataclass(frozen=True)
class ViewSnapshot:
    """A scope's live context at one moment.

    Attributes:
        generation: Renewed (process-unique) by every recorded op on the scope and
            every rebuild; appends keep it, so a reader that saw generation ``g`` with
            ``n`` segments may treat the first ``n`` segments of a later snapshot with
            the same ``g`` as unchanged.
        segments: The live segments in render order.
    """

    generation: int
    segments: tuple[Segment, ...]


def retirements(atoms: Iterable[Segment], scope: str) -> dict[str, int]:
    """When each retired atom of ``scope`` was retired: ``{atom id: clock}``.

    A ``summarize`` / ``replace`` producer retires its ``derived_from`` at its own
    clock; a ``delete`` op record retires its targets at the op's clock. The earliest
    retirement wins.
    """
    tomb: dict[str, int] = {}

    def _retire(ids: Iterable[str], lt: int) -> None:
        for atom_id in ids:
            cur = tomb.get(atom_id)
            if cur is None or lt < cur:
                tomb[atom_id] = lt

    for a in atoms:
        if a.scope != scope:
            continue
        if a.kind == WS_OP_KIND:
            if a.content.get("op") == "delete":
                _retire(list(a.content.get("targets") or []), a.logical_time)
        elif a.kind != STEP_OPEN_KIND and a.derived_from:
            _retire(list(a.derived_from), a.logical_time)
    return tomb


def fold_atoms(
    atoms: Sequence[Segment],
    scope: str,
    *,
    as_of: int | None,
    include_tombstoned: bool,
) -> list[Segment]:
    """Fold content-lane atoms into ``scope``'s ordered segment view -- THE fold.

    Content atoms of ``scope`` are ordered by ``(order, logical_time)``; a target is
    retired at the clock of the op or producer that retired it (:func:`retirements`).
    ``as_of`` yields the view as it was at that clock (atoms created after it are
    unborn; a retirement after it has not landed yet).

    Args:
        atoms: Lane atoms (any scope, any kind); only ``scope``'s are folded.
        scope: The logical working-set scope.
        as_of: Optional ``logical_time`` upper bound (``None`` = the live view).
        include_tombstoned: Keep retired atoms, marked ``tombstoned`` at their
            retirement clock (the history view); otherwise drop them (the live view).

    Returns:
        The folded segments in render order.
    """
    tomb = retirements(atoms, scope)
    content = [a for a in atoms if a.scope == scope and a.kind not in NON_CONTENT_KINDS]
    out: list[Segment] = []
    for a in sorted(content, key=lambda s: (s.order, s.logical_time)):
        if as_of is not None and a.logical_time > as_of:
            continue
        retired_at = tomb.get(a.id)
        retired = retired_at is not None and (as_of is None or retired_at <= as_of)
        if not retired:
            out.append(a)
        elif include_tombstoned:  # the history view says it is retired, and when
            out.append(msgspec.structs.replace(a, status="tombstoned", tombstoned_at=retired_at))
    return out


def order_at(
    lowest: float | None, highest: float | None, live: Sequence[Segment], position: int
) -> float:
    """The gap-allocated ``order`` for a segment placed at render ``position``.

    Args:
        lowest: The lowest ``order`` over every content segment of the scope (any
            status), or ``None`` when it has none.
        highest: The highest such ``order``, or ``None``.
        live: The live segments in render order.
        position: 0-based render position over ``live``.

    Returns:
        Below everything at position 0, above everything at the end, else the midpoint
        of the two neighbours (a mid-insert never renumbers them).
    """
    if position <= 0:
        lo = lowest if lowest is not None else 1.0
        return lo - 1.0 if live else 1.0
    if position >= len(live):
        return (highest if highest is not None else 0.0) + 1.0
    return (live[position - 1].order + live[position].order) / 2.0


class ContextView:
    """The folded live context of one logical scope, from its anchor on.

    Holds the scope's atoms at or after the anchor (content and ``delete`` op
    records), the live segments in render order, the retirement map and the order
    bounds. Mutated only under :attr:`lock` by the owning store.
    """

    def __init__(self, scope: str, anchor: AnchorEntry | None, atoms: Iterable[Segment]) -> None:
        """Build the view from the scope's atoms (any atoms before the anchor are dropped).

        Args:
            scope: The logical working-set scope.
            anchor: The scope's anchor, or ``None`` (the view starts at the beginning).
            atoms: Lane atoms read for the scope (other scopes and breadcrumbs ignored).
        """
        self.scope = scope
        self.anchor = anchor
        self.lock = threading.RLock()
        self.generation = 0
        since = anchor.logical_time if anchor is not None else 0
        self._atoms = sorted(
            (
                a
                for a in atoms
                if a.scope == scope and a.kind != STEP_OPEN_KIND and a.logical_time >= since
            ),
            key=lambda s: s.logical_time,
        )
        self.min_order: float | None = anchor.min_order if anchor is not None else None
        self.max_order: float | None = anchor.max_order if anchor is not None else None
        for a in self._atoms:
            self._bound(a)
        self.live: list[Segment] = []
        self.retired: dict[str, int] = {}
        self._rebuild()

    @property
    def next_order(self) -> float:
        """``append``'s order: one above every content atom the scope ever had."""
        return (self.max_order if self.max_order is not None else 0.0) + 1.0

    def order_for_position(self, position: int) -> float:
        """``insert``'s gap-allocated order at render ``position``."""
        return order_at(self.min_order, self.max_order, self.live, position)

    def snapshot(self) -> ViewSnapshot:
        """The live segments now, with the generation they belong to."""
        with self.lock:
            return ViewSnapshot(self.generation, tuple(self.live))

    def append(self, atom: Segment) -> None:
        """Add an appended content atom at the end of the live list (no fold).

        Raises:
            ValueError: ``atom`` is not a content atom of this scope or would not render
                last (an append's order is above every other).
        """
        if atom.scope != self.scope or atom.kind in NON_CONTENT_KINDS:
            raise ValueError(f"not a content atom of scope {self.scope!r}: {atom.kind}")
        if self.max_order is not None and atom.order <= self.max_order:
            raise ValueError(f"an appended atom must render last (order {atom.order})")
        self._atoms.append(atom)
        self._bound(atom)
        self.live.append(atom)

    def apply_op(self, atom: Segment) -> None:
        """Take a recorded op's atom (a producer or an op record) and rebuild."""
        self._atoms.append(atom)
        self._bound(atom)
        self._rebuild()

    def anchors_at(self, summary: Segment) -> bool:
        """Whether ``summary`` retired every content atom of the scope created before it.

        Atoms before the current anchor are retired by construction; the view holds
        every later one, so checking those is exact.
        """
        for a in self._atoms:
            if a.logical_time >= summary.logical_time:
                break
            if a.kind in NON_CONTENT_KINDS:
                continue
            retired_at = self.retired.get(a.id)
            if retired_at is None or retired_at > summary.logical_time:
                return False
        return True

    def move_anchor(self, anchor: AnchorEntry) -> None:
        """Start the view at ``anchor`` (its live list is unchanged: all before is retired)."""
        self.anchor = anchor
        self._atoms = [a for a in self._atoms if a.logical_time >= anchor.logical_time]
        self.retired = retirements(self._atoms, self.scope)

    def is_retired(self, atom_id: str, logical_time: int) -> bool:
        """Whether an atom of this scope is retired (before the anchor, or retired since)."""
        if self.anchor is not None and logical_time < self.anchor.logical_time:
            return True
        return atom_id in self.retired

    def _bound(self, atom: Segment) -> None:
        if atom.kind in NON_CONTENT_KINDS:
            return
        if self.min_order is None or atom.order < self.min_order:
            self.min_order = atom.order
        if self.max_order is None or atom.order > self.max_order:
            self.max_order = atom.order

    def _rebuild(self) -> None:
        self.live = fold_atoms(self._atoms, self.scope, as_of=None, include_tombstoned=False)
        self.retired = retirements(self._atoms, self.scope)
        self.generation = next_generation()
