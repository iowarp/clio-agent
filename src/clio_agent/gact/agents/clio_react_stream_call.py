"""One streamed model request, with cancellation and safe interruption detection."""

from __future__ import annotations

import contextlib
import logging
import math
from typing import Any

import anyio
import dspy
from dspy.lm15 import LM15Error, Request, Response
from dspy.utils.exceptions import LMUnexpectedError

from clio_agent.gact.agents import clio_react_stream as stream
from clio_agent.gact.agents.clio_react_recovery import StreamProgress, interrupted_stream
from clio_agent.lm.engines.lm_loop import run_on_lm_loop

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# The streamed LM call                                                        #
# --------------------------------------------------------------------------- #
#: How often an in-flight LM call checks the turn's cooperative cancellation.
_CANCEL_POLL_SECONDS = 0.25


def call_lm(lm: Any, request: Request) -> Response:
    """One streamed call: text and thinking reach the live lanes as they arrive."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.clio_react import (  # noqa: PLC0415
        _raise_if_cancelled,
        _turn_cancelled,
    )
    from clio_agent.runtime.lm_activity import note_lm_call_start  # noqa: PLC0415

    session_id = _ctx.active_session_id() or ""
    progress = StreamProgress()
    # Each model call's text is its own part: the boundary is ordered after the
    # previous call's deltas and before this call's first one.
    note_lm_call_start()

    async def run() -> Response:
        send, receive = anyio.create_memory_object_stream(math.inf)
        response: Response | None = None
        failure: Exception | None = None
        router = stream.StreamRouter()
        consumed = anyio.Event()

        async def consume() -> None:
            async with receive:
                async for chunk in receive:
                    progress.emitted = True
                    stream.route_pieces(progress.observe(router.feed(chunk)))
            stream.route_pieces(progress.observe(router.finish()), unclosed=router.incomplete)
            consumed.set()

        async def watch_cancel() -> None:
            # A cancel lands mid-call too (F038): raising here cancels the call's
            # scope, which closes the HTTP stream so the server stops generating
            # instead of finishing a prefill and a full answer nobody reads.
            while not finished.is_set():
                with anyio.move_on_after(_CANCEL_POLL_SECONDS):
                    await finished.wait()
                if not finished.is_set():
                    _raise_if_cancelled()

        finished = anyio.Event()
        handle = None
        try:
            async with anyio.create_task_group() as group:
                handle = stream.register_stream(lm, session_id, group.cancel_scope)
                group.start_soon(consume)
                group.start_soon(watch_cancel)
                try:
                    with dspy.context(send_stream=send):
                        owner = getattr(
                            getattr(_ctx.active_app(), "state", None), "conversations", None
                        )
                        slot = (
                            owner.inference_slot(session_id)
                            if owner is not None
                            else contextlib.nullcontext()
                        )
                        async with slot:
                            response = await lm.acall(request)
                except Exception as exc:
                    failure = exc
                    logger.warning(
                        "model stream failed reason=lm_stream_drain_before_raise error_type=%s",
                        type(exc).__name__,
                    )
                    # Let the consumer finish before this error cancels the task
                    # group. Keep a direct re-raise: failures never become results.
                    finished.set()
                    await send.aclose()
                    await consumed.wait()
                    raise
                finally:
                    finished.set()
                    await send.aclose()
                    stream.unregister_stream(handle)
        except BaseExceptionGroup as group_error:
            # The task group wraps the call's own error; the caller handles it typed.
            # (Not ``from None``: that would erase the leaf's own ``__cause__``.)
            leaf = _sole(group_error)
            if (
                leaf is failure
                and isinstance(leaf, Exception)
                and (interrupted := interrupted_stream(lm, request, progress, leaf))
            ):
                raise interrupted from leaf
            leaf.__suppress_context__ = True
            raise leaf  # noqa: B904 - the leaf keeps its own cause chain
        if response is None and handle is not None and handle.aborted:
            raise _turn_cancelled()  # the session cancel closed the stream (abort handle)
        assert response is not None
        return response

    try:
        # One persistent LM loop: connections survive across steps, turns and agents.
        return run_on_lm_loop(run)
    except LMUnexpectedError as exc:
        # DSPy wraps an engine's own typed error (a refused sign-in, an exhausted plan,
        # a Codex transport failure) as unexpected; the turn classifies the original.
        cause = exc.__cause__
        if isinstance(cause, Exception) and not isinstance(cause, LM15Error):
            raise cause from exc
        raise


def _sole(group: BaseExceptionGroup) -> BaseException:
    """The one leaf error of a task-group failure (the group itself when there are more)."""
    leaves: list[BaseException] = []
    pending: list[BaseException] = [group]
    while pending:
        exc = pending.pop()
        if isinstance(exc, BaseExceptionGroup):
            pending.extend(exc.exceptions)
        else:
            leaves.append(exc)
    return leaves[0] if len(leaves) == 1 else group
