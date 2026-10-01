"""A context op names live segments, or it fails typed (owner module).

Delete, summarize and replace used to skip any id that was not live -- never written,
already deleted or compacted away -- so a human's or an algorithm's edit could silently
not happen. Every op now checks its ids against the scope's live set first and applies
nothing when one is stale.
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


def require_live(ids: Iterable[str], live_ids: Collection[str], *, op: str, scope: str) -> None:
    """Raise :class:`StaleSegmentIdError` unless ``ids`` is non-empty and all are live."""
    wanted = list(ids)
    missing = [i for i in wanted if i not in live_ids] or ([] if wanted else ["<none>"])
    if missing:
        raise StaleSegmentIdError(op=op, scope=scope, missing=missing)
