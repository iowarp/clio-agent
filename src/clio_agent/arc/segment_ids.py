"""A context op names live segments, or it fails typed (owner module).

Delete, summarize and replace check their ids against the scope's live set first and
apply nothing when one is not live (never written, already deleted or compacted away).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from clio_agent.errors import ClioError


class StaleSegmentIdError(ClioError):
    """A context op named ids that are not live in the scope; nothing was applied."""

    reason = "stale_segment_id"

    def __init__(self, *, op: str, scope: str, missing: list[str]) -> None:
        super().__init__(
            f"context {op} on scope {scope!r} names {len(missing)} id(s) that are not live "
            f"(never written, already deleted or compacted away): {', '.join(missing)}",
            error_type=self.reason,
            details={"op": op, "scope": scope, "missing": missing},
        )


class ContextOpLogError(ClioError):
    """A context op's ``arc.op`` record could not be written; the op was not applied.

    The trace is what replays clio-core's context (``reconstruct_arc_segments``): an op
    applied without it would leave that replay silently incomplete.
    """

    reason = "context_op_log_failed"

    def __init__(self, *, op: str, scope: str, cause: BaseException) -> None:
        super().__init__(
            f"context {op} on scope {scope!r} was not applied: its trace record failed "
            f"({type(cause).__name__}: {cause})",
            error_type=self.reason,
            details={"op": op, "scope": scope, "cause": type(cause).__name__},
        )


def require_live(ids: Iterable[str], live_ids: Collection[str], *, op: str, scope: str) -> None:
    """Raise :class:`StaleSegmentIdError` unless ``ids`` is non-empty and all are live."""
    wanted = list(ids)
    missing = [i for i in wanted if i not in live_ids] or ([] if wanted else ["<none>"])
    if missing:
        raise StaleSegmentIdError(op=op, scope=scope, missing=missing)
