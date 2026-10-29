"""Typed turn failures of the agent's ARC live-plane context: read, fold and write.

Re-exported by :mod:`clio_agent.gact.agents.clio_react_record`, which records and reads
that context.
"""

from __future__ import annotations

from typing import Any

from clio_agent.errors import ClioError


class ContextReadError(ClioError):
    """The loop could not read its context from the ARC live plane (typed turn failure)."""

    reason = "arc_context_read_failed"

    def __init__(self, scope: str, cause: BaseException) -> None:
        super().__init__(
            f"could not read the agent context for scope {scope!r}: {cause}",
            error_type=self.reason,
            details={"scope": scope, "cause": type(cause).__name__},
        )


class ContextFoldError(ClioError):
    """A clio-core record could not be folded into the agent's messages (typed turn failure).

    The fold never guesses: an unmodelled kind, malformed content, an observation that
    answers no open call, a call left unanswered or a summary with no text would each
    silently change what the model sees.
    """

    reason = "context_fold_failed"

    def __init__(self, problem: str, segment: Any, detail: str = "") -> None:
        seg_id = str(getattr(segment, "id", "") or "")
        kind = str(getattr(segment, "kind", "") or "")
        super().__init__(
            f"cannot fold the agent context: {problem} at segment {seg_id or '?'} ({kind})"
            + (f": {detail}" if detail else ""),
            error_type=self.reason,
            details={"problem": problem, "segment_id": seg_id, "kind": kind},
        )


class ContextWriteError(ClioError):
    """The loop could not record a message on the ARC live plane (typed turn failure).

    The plane is the conversation: a lost write would leave the model a context that
    silently lacks what just happened, so it fails the turn instead.
    """

    reason = "arc_context_write_failed"

    def __init__(self, scope: str, kind: str, cause: BaseException) -> None:
        super().__init__(
            f"could not record the agent's {kind} for scope {scope!r}: {cause}",
            error_type=self.reason,
            details={"scope": scope, "kind": kind, "cause": type(cause).__name__},
        )
