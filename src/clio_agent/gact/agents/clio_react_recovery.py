"""Bounded recovery of stateless generation before answer text or tool execution.

DSPy owns retries before it forwards a chunk. Once a stream has started, only
the agent loop knows that proposed function calls have not executed. Recovery
here never repeats a step or replays completed tools. Partial reasoning stays
in the transcript, separated from the new attempt by a service notice.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dspy.lm15 import FunctionTool, Request, Response
from dspy.utils.exceptions import LMTimeoutError, LMTransportError

from clio_agent.gact.reasoning_extract import ANSWER, Piece


def transport_failure(exc: BaseException) -> LMTransportError | LMTimeoutError | None:
    """Find a typed model transport error without guessing from arbitrary text."""
    seen: set[int] = set()
    pending = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (LMTransportError, LMTimeoutError)):
            return current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return None


@dataclass
class StreamProgress:
    """Whether this attempt forwarded a chunk or any public answer text."""

    emitted: bool = False
    answer: bool = False

    def observe(self, pieces: list[Piece]) -> list[Piece]:
        """Track routed answer text without retaining another copy of the stream."""
        self.answer |= any(p.kind == ANSWER and bool(p.text) for p in pieces)
        return pieces


class InterruptedModelStream(Exception):
    """A failed generation that has no accepted answer or executed tool calls."""

    def __init__(self, original: Exception) -> None:
        super().__init__(str(original))
        self.original = original


def interrupted_stream(
    lm: Any, request: Request, progress: StreamProgress, exc: Exception
) -> InterruptedModelStream | None:
    """Recognize only opted-in, typed transport failures before public prose.

    Custom/stateful engines and provider-native tools own their own recovery. A
    stream that never forwarded a chunk has already used DSPy's retry policy.
    """
    from clio_agent.lm.hooked_lm import effective_lm_for_call  # noqa: PLC0415

    target = effective_lm_for_call(lm, lm)
    engine = getattr(target, "_engine_spec", None)
    if (
        getattr(target, "_clio_stream_recovery", False) is True
        and isinstance(engine, str)
        and engine in {"auto", "lm15"}
        and progress.emitted
        and not progress.answer
        and isinstance(exc, (LMTransportError, LMTimeoutError))
        and all(isinstance(tool, FunctionTool) for tool in request.tools)
    ):
        return InterruptedModelStream(exc)
    return None


def _notice(attempt: int, maximum: int, delay: float) -> None:
    from clio_agent.gact import context as ctx  # noqa: PLC0415
    from clio_agent.gact.parts import Part  # noqa: PLC0415
    from clio_agent.gact.tool_observer import (  # noqa: PLC0415
        _append_live_assistant_part,
    )
    from clio_agent.runtime.lm_activity import after_live_chunks  # noqa: PLC0415

    app, sid = ctx.active_app(), ctx.active_session_id()
    if app is None or not sid:
        return
    part = Part(
        id=f"notice_{uuid.uuid4().hex[:12]}",
        type="notice",
        source="model_connection_retry",
        agent_id=ctx.active_react_scope() or "",
        text=(
            f"The model connection was interrupted before an answer arrived. "
            f"Retrying in {delay:g}s ({attempt}/{maximum}). Completed tools will not be replayed."
        ),
        metadata=ctx.stamp_active_try(
            {"actor": "algorithm", "retry_attempt": attempt, "retry_limit": maximum}
        ),
    )
    # Emissions were scheduled cross-thread. Append after those callbacks, so
    # the failed reasoning passage cannot land after its retry boundary.
    after_live_chunks(lambda: _append_live_assistant_part(app, sid, part))


def recover_stream(
    lm: Any,
    request: Callable[[], Request],
    call: Callable[[Any, Request], Response],
    check_cancel: Callable[[], None],
) -> Response:
    """Retry only the interrupted generation, honoring cancellation and new input."""
    from clio_agent.lm.policy import lm_retries  # noqa: PLC0415
    from clio_agent.runtime.stream_audit import stream_audit  # noqa: PLC0415

    maximum = lm_retries()
    for attempt in range(maximum + 1):
        check_cancel()
        try:
            return call(lm, request())
        except InterruptedModelStream as exc:
            check_cancel()
            if attempt == maximum:
                raise exc.original from exc.original.__cause__
            delay = min(2.0**attempt, 8.0)
            stream_audit(
                "lm.stream.retry",
                reason="model_stream_interrupted",
                attempt=attempt + 1,
                retry_limit=maximum,
                delay_seconds=delay,
                original_error=type(exc.original).__name__,
            )
            _notice(attempt + 1, maximum, delay)
            deadline = time.monotonic() + delay
            while (remaining := deadline - time.monotonic()) > 0:
                check_cancel()
                time.sleep(min(0.25, remaining))
    raise AssertionError("retry loop must return or raise")
