"""Typed failures at the ARC segment persistence boundary."""

from clio_agent.errors import ClioError


class SegmentReadError(ClioError):
    """A persisted segment scope cannot be decoded into its expected record list."""

    def __init__(self, session_id: str, scope: str) -> None:
        super().__init__(
            f"ARC segments for session {session_id!r}, scope {scope!r} cannot be decoded",
            error_type="clio_core_segments_invalid",
            details={"session_id": session_id, "scope": scope},
        )


class ArcPersistError(ClioError):
    """clio-core did not accept a write; the in-memory copy was discarded."""

    def __init__(self, session_id: str, scope: str, cause: BaseException) -> None:
        self.scope = scope
        super().__init__(
            f"clio-core did not store the write to {session_id}/{scope}: {cause}",
            error_type="arc_persist_failed",
            details={"session_id": session_id, "scope": scope, "cause": type(cause).__name__},
        )
