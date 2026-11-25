"""Recover from a request that outgrew the model's context window: compact, then retry.

Auto compaction runs between steps when the measured prompt crosses the session's
threshold of the context window (:func:`clio_agent.gact.compaction.maybe_autocompact`).
That cannot help when the window is unknown or wrong (a catalog maximum the provider
does not serve), or when one step's results jump past the window at once. The model
then refuses the request (``ContextWindowExceededError``/``ContextLengthError``), and the
loop used to stop with ``context_window_exceeded``: the turn ended with tool results
and no reply, though nothing was lost but room.

Here the running scope is compacted instead, and the loop sends the step again:

- every step is summarized, the last one too (:func:`overflow_keep_policy`), since the
  last step is usually what overflowed;
- the summary is written from a copy of the context in which each tool result is cut
  to :data:`SUMMARY_RESULT_CHARS` (saying so), because the whole context is what did
  not fit; it is asked outside any provider conversation, on the loop's model;
- a compaction that cannot run or fails is reported typed (the trace and the stream
  audit), and the loop then stops with ``context_window_exceeded`` as before.

The loop retries once per step: a second refusal after a compaction stops the turn.
"""

from __future__ import annotations

from typing import Any

from clio_agent.runtime import trace
from clio_agent.runtime.stream_audit import stream_audit

#: Traced when a refused request was compacted and the step is sent again.
OVERFLOW_COMPACTED = "context_overflow_compacted"
#: Traced when a refused request could not be compacted (reason in the record).
OVERFLOW_NOT_COMPACTED = "context_overflow_not_compacted"
#: Characters of one tool result kept in the copy the overflow summary is written from.
SUMMARY_RESULT_CHARS = 4_000


def _report(stage: str, session: str, /, **fields: Any) -> None:
    trace.event(
        "COMPACT",
        "%s session=%s %s",
        stage,
        session,
        " ".join(f"{k}={v}" for k, v in fields.items()),
    )
    stream_audit(stage, session_id=session, **fields)


def compact_after_overflow(guard: Any, lm: Any) -> bool:
    """Compact the running scope after the model refused a too-long request.

    Args:
        guard: The loop's :class:`~clio_agent.gact.compaction.AutoCompactionGuard`; its
            compaction id is updated so the loop re-offers working state.
        lm: The loop's model, which writes the summary outside its conversation.

    Returns:
        Whether the context was compacted (the step can be sent again).
    """
    from clio_agent.arc import history_mode  # noqa: PLC0415
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react_record import arc_scope  # noqa: PLC0415
    from clio_agent.gact.compaction import CompactionError, compact_session_context  # noqa: PLC0415
    from clio_agent.gact.compaction_policy import overflow_keep_policy  # noqa: PLC0415
    from clio_agent.gact.compaction_summarizer import scope_summarizer  # noqa: PLC0415
    from clio_agent.providers.stateful_common import outside_stateful_scope  # noqa: PLC0415

    arc, session, scope = arc_scope()
    app = _ctx.active_app()
    if arc is None or app is None:
        _report(OVERFLOW_NOT_COMPACTED, session, reason="no_context_store")
        return False
    if history_mode.active():
        _report(OVERFLOW_NOT_COMPACTED, session, reason="history_mode")
        return False
    ask = scope_summarizer(app, session, scope, lm, clip_results=SUMMARY_RESULT_CHARS)

    def summarize(prompt: str) -> str:
        with outside_stateful_scope():  # not a step of the agent's conversation
            return ask(prompt)

    try:
        with overflow_keep_policy():
            result = compact_session_context(
                app, session, trigger="auto", scope=scope, summarize=summarize
            )
    except CompactionError as exc:
        _report(OVERFLOW_NOT_COMPACTED, session, reason=exc.error, message=exc.message)
        return False
    if not result.get("compacted"):
        _report(OVERFLOW_NOT_COMPACTED, session, reason="nothing_to_compact")
        return False
    guard.compaction_id = str(result["compactions"][-1]["compaction_id"])
    guard.awaiting_measurement = True
    _report(OVERFLOW_COMPACTED, session, scope=scope, compaction_id=guard.compaction_id)
    return True
