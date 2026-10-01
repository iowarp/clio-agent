"""The ARC live context plane surface of :class:`~clio_agent.arc.memory.ARCMemory`.

``ARCMemory`` composes the segment store the ReAct loop reads its context from; this
mixin is that surface (append / insert / delete / summarize / replace, the renders,
the context view, scope discovery and search), kept in its own owner module so
``memory.py`` stays the record-cache facade.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, cast

from clio_agent.arc.schema import SegmentKind
from clio_agent.arc.segments import OpLogger, SegmentStore
from clio_agent.errors import ClioError

__all__ = ["SearchUnavailableError", "SegmentPlane"]


class SearchUnavailableError(ClioError):
    """clio-core cannot search this deployment's context (typed reason)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(
            f"clio-core cannot search this context (reason={reason})",
            error_type="search_unavailable",
            details={"reason": reason},
        )


class SegmentPlane:
    """The live context plane methods of ``ARCMemory`` (over ``self._segments``)."""

    _segments: SegmentStore

    # ---- Live context plane (the segment store the ReAct loop reads from) ----

    def set_segment_op_logger(self, op_logger: "OpLogger | None") -> None:
        """Inject the durable-Trace op logger into the segment store.

        Called by the gact app once both the app handle and ARC exist, so each
        applied context op is mirrored to the Trace. Keeps ``arc/`` free of any
        ``gact/`` import.
        """
        self._segments.set_op_logger(op_logger)

    def append_segment(
        self,
        session_id: str,
        scope: str,
        kind: str,
        content: Dict[str, Any],
        *,
        step: int = -1,
        trace_ref: str = "",
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Any:
        """Append one segment to a scope's live context (append = insert at end).

        ``turn_id`` / ``expert_span_id`` / ``run_span_id`` are optional
        trajectory-correlation span ids stamped on the new segment (default ``""``)."""
        return self._segments.append(
            session_id,
            scope,
            cast(SegmentKind, kind),
            content,
            step=step,
            trace_ref=trace_ref,
            token_count=token_count,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )

    def insert_segment(
        self,
        session_id: str,
        scope: str,
        position: int,
        kind: str,
        content: Dict[str, Any],
        *,
        step: int = -1,
        trace_ref: str = "",
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Any:
        """Insert one segment at a render position in a scope's live context.

        ``turn_id`` / ``expert_span_id`` / ``run_span_id`` are optional
        correlation span ids stamped on the new segment (default ``""``)."""
        return self._segments.insert(
            session_id,
            scope,
            position,
            cast(SegmentKind, kind),
            content,
            step=step,
            trace_ref=trace_ref,
            token_count=token_count,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )

    def delete_segments(self, session_id: str, scope: str, ids: List[str]) -> int:
        """Tombstone segments by id (skipped by render, kept for replay)."""
        return self._segments.delete(session_id, scope, ids)

    def summarize_segments(
        self,
        session_id: str,
        scope: str,
        ids: List[str],
        summary_content: Dict[str, Any],
        *,
        trace_ref: str = "",
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Any:
        """Replace a range of segments with one summary (= context-compaction over all).

        ``turn_id`` / ``expert_span_id`` / ``run_span_id`` are optional correlation
        span ids stamped on the summary segment (default ``""``)."""
        return self._segments.summarize(
            session_id,
            scope,
            ids,
            summary_content,
            trace_ref=trace_ref,
            token_count=token_count,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )

    def replace_segment(
        self,
        session_id: str,
        scope: str,
        target_id: str,
        content: Dict[str, Any],
        *,
        kind: Optional[str] = None,
        trace_ref: str = "",
        token_count: int = 0,
        turn_id: str = "",
        expert_span_id: str = "",
        run_span_id: str = "",
    ) -> Any:
        """Replace a live segment's content in place (1:1 supersede at the same render
        slot; the original is tombstoned + recoverable as-of-T).

        ``kind`` defaults to the original's kind; the correlation span ids default to
        the ORIGINAL's (a pure content edit stays in the same turn/expert/run). Returns
        the new Segment, or ``None`` if ``target_id`` matched no live segment."""
        return self._segments.replace(
            session_id,
            scope,
            target_id,
            content,
            kind=cast(Optional[SegmentKind], kind),
            trace_ref=trace_ref,
            token_count=token_count,
            turn_id=turn_id,
            expert_span_id=expert_span_id,
            run_span_id=run_span_id,
        )

    def apply_segment_op(self, op: str, session_id: str, scope: str, **kwargs: Any) -> Any:
        """Stable dispatch over the five ops — the KV-backend swap seam."""
        return self._segments.apply(op, session_id, scope, **kwargs)

    def render_segments(self, session_id: str, scope: str, *, as_of: Optional[int] = None) -> Any:
        """Ordered LIVE segments for a scope (the decisive read; as-of-T optional)."""
        return self._segments.render(session_id, scope, as_of=as_of)

    def list_segments(
        self, session_id: str, scope: str, *, include_tombstoned: bool = False
    ) -> Any:
        """A scope's segments in order; ``include_tombstoned`` keeps retired ones (history)."""
        return self._segments.list_segments(
            session_id, scope, include_tombstoned=include_tombstoned
        )

    def render_working_set(
        self, session_id: str, scope: str, *, as_of: Optional[int] = None
    ) -> Any:
        """Ordered LIVE working-set segments: the agent's context (compaction's target)."""
        return self._segments.render_working_set(session_id, scope, as_of=as_of)

    def render_segment_text(
        self, session_id: str, scope: str, *, as_of: Optional[int] = None
    ) -> str:
        """The live segments flattened to text (inspection / byte-equality)."""
        return self._segments.render_text(session_id, scope, as_of=as_of)

    def segment_tokens_by_kind(self, session_id: str, scope: str) -> Dict[str, int]:
        """Per-kind token attribution for a scope's live segments (compaction targeting)."""
        return self._segments.tokens_by_kind(session_id, scope)

    def list_segment_scopes(self, session_id: str, scope_prefix: str = "") -> List[str]:
        """Scopes that have context in this session (for discovery / a scope picker)."""
        return self._segments.scan_scopes(session_id, scope_prefix)

    def search_segment_scopes(
        self, session_id: str, query_text: str, *, scope_prefix: str = "", k: int = 10
    ) -> List[Any]:
        """Rank a session's scopes by relevance to ``query_text`` (BM25 on clio-core).

        Raises:
            SearchUnavailableError: clio-core cannot search (#905: the indexer chimod is
                absent) -- never an empty list a caller could read as "nothing found".
        """
        if not self._segments.supports_search():
            raise SearchUnavailableError(self._segments.search_degradation_reason())
        return self._segments.search_scopes(session_id, query_text, scope_prefix=scope_prefix, k=k)

    def segment_search_is_semantic(self) -> bool:
        """Whether scope search uses real BM25 (clio-core backend) vs the naive fallback."""
        return self._segments.supports_search()

    def segment_search_degradation_reason(self) -> str:
        """Typed reason real search is degraded (#905), or ``""`` when fully available."""
        return self._segments.search_degradation_reason()
