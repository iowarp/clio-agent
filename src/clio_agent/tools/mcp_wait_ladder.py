"""MCP wait semantics: typed backstops + surfaced waits (#1282, campaign C1-S2 D3).

The owner module for the wait-side of the slice (D1/D2 own refusal semantics —
``tools/mcp_errors.py`` / ``gact/agents/reactv2.py``). Copies the SHAPE of
``arc/rpc_liveness.py``'s per-RPC stall ladder — typed per-attempt structured
surfacing, never a bare exception — with the ONE deliberate divergence the
slice spec calls out: **no terminal give-up on slowness**. ``arc/rpc_liveness``
itself is untouched (ARC's ladder is out of scope, #1282's own constraint).

Four pieces (adversarial-review round, F1/F3/F4):

1. :func:`typed_call_timeout_error` / :func:`typed_task_drive_timeout_error` —
   type the two remaining bare-``TimeoutError`` firing sites
   (``tools/mcp_executor.py``'s executor backstop; ``tools/mcp_tasks.py``'s
   ``_poll_until_terminal`` deadline) into a :class:`MCPCallTimeoutBackstopError`
   that is BOTH a real :class:`~clio_agent.errors.ToolError`/``ClioError`` (so
   ``to_dict()`` reaches the wire/trace, F4) AND a ``TimeoutError`` (so the
   executor's existing ``isinstance(exc, TimeoutError)`` retry-safety
   classification is unaffected — verified both ways in
   ``tests/test_tools/test_mcp_wait_ladder.py``). Firing is ALWAYS
   ``logger.warning``'d (never opt-in-only) plus ``stream_audit``'d (opt-in
   structured detail) — never silent, never JSONL-only.

   ``call_timeout_s`` (F3a) is EXPLICITLY SANCTIONED to remain a hard,
   terminal bound — but NOT a flat wall clock over the whole call: see
   :func:`run_with_activity_backstop`. ``_poll_until_terminal``'s OWN
   deadline (F3b) has NO legitimate producer anywhere in clio's own call
   sites today (``mcp_task_extension.py``'s ``ctx.read_timeout_seconds`` is
   always ``None`` — nothing in this codebase ever passes
   ``Client.call_tool(..., read_timeout_seconds=...)``; only clio-relay's own
   ``resume()`` forwards an optional caller value, out of this slice's scope
   to audit further). Typing it (rather than declaring it unbounded) is the
   conservative choice: it costs nothing on the always-None path and stays a
   correct, typed backstop for whatever relay (or a future caller) passes.

2. :func:`ActivityClock` / :func:`run_with_activity_backstop` — the F3a fix:
   ``call_timeout_s`` bounds ``last_activity + call_timeout_s``, not call
   START + call_timeout_s. A flat ``asyncio.wait_for`` cancels genuinely
   progressing work (the call path is NOT instrument-less — progress
   notifications and, for a task-mode drive, status-transition polls both
   exist) — that violates the six wait constraints' rule 4 even though the
   backstop itself is sanctioned to remain terminal. Progress
   (:func:`touch_active_activity_clock`, called by the executor's
   ``progress_handler`` AND by :func:`default_task_wait_observer` on every
   non-terminal poll) resets the deadline; a genuinely SILENT call still
   hits the typed backstop. Deliberately does NOT restructure
   ``AsyncMCPToolExecutor._call_lock`` (the reviewer's alternative,
   skipped): the lock bounds how long clio itself holds one executor's
   single in-flight slot, not how long a server may legitimately take to
   answer a PROGRESSING call — activity-reset defuses starvation for the
   progressing case without touching that invariant, and a silent call
   still frees the lock at ``call_timeout_s``, same as before.

3. :func:`default_task_wait_observer` — the per-drive ``on_poll`` hook every
   NON-relay backend was missing entirely (rule 5, "every wait names what it
   waits on," was silently unmet for it). F1 fix: emits the lean
   ``mcp_task.wait`` event (transient — see
   :mod:`clio_agent.gact.mcp_task_events` — it is connection-timeline
   plumbing, never replay history, exactly like ``server.heartbeat``) ONLY on
   a status CHANGE or after a >=1s throttle window, never once per raw poll
   (measured ~7.2k events/hr un-throttled against a 50ms task poll interval —
   the exact #761 heartbeat-flood regression). The attempt counter itself
   still advances on every OBSERVED poll (never gated) so a surfaced wait's
   "attempt N" is the true poll count, not the emitted-event count.
   ``tools/task_observers.py::resolve_task_observer`` falls back to this when
   no backend-specific factory is registered for a drive's ``server_id``.

The async-tool-callable guard for D1's refusal-marking wrap lives in
``gact/agents/reactv2.py`` (F2), not here — see that module's docstring for
why an async ``dspy.Tool`` callable is refused rather than "handled."
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

from clio_agent.errors import ToolError
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastmcp_tasks.client_models import ClientGetTaskResult

    from clio_agent.tools.mcp_task_records import TaskKey, TaskRecordStore
    from clio_agent.tools.mcp_tasks import OnPollHook

logger = logging.getLogger(__name__)

__all__ = [
    "MCP_CALL_TIMEOUT_BACKSTOP",
    "MCP_TASK_DRIVE_TIMEOUT_BACKSTOP",
    "MCPCallTimeoutBackstopError",
    "ActivityClock",
    "default_task_wait_observer",
    "run_with_activity_backstop",
    "touch_active_activity_clock",
    "typed_call_timeout_error",
    "typed_task_drive_timeout_error",
]

_T = TypeVar("_T")

#: #1282 D3: the ``tools.mcp.call_timeout_s`` runaway backstop fired. A
#: documented ceiling of last resort, not a wait-ladder verdict on slowness —
#: see the module docstring.
MCP_CALL_TIMEOUT_BACKSTOP = "mcp_call_timeout_backstop"

#: #1282 F3b: ``mcp_tasks._poll_until_terminal``'s OWN (normally-unproduced)
#: deadline fired.
MCP_TASK_DRIVE_TIMEOUT_BACKSTOP = "mcp_task_drive_timeout_backstop"


class MCPCallTimeoutBackstopError(ToolError, TimeoutError):
    """A typed, wire-visible MCP timeout backstop firing (#1282 F3/F4).

    Real multiple inheritance, not a wrapper: ``ToolError`` (a ``ClioError`` —
    ``to_dict()`` reaches ``ErrorInfo``/the trace) AND ``TimeoutError`` (so
    every existing ``isinstance(exc, TimeoutError)`` classification —
    ``tools/execution.py``'s retry-safety / uncertain-mutating-timeout check
    chief among them — keeps working byte-identically). MRO-verified:
    ``isinstance`` holds for BOTH bases; ``str(exc)`` and ``exc.to_dict()``
    both work (tested in ``tests/test_tools/test_mcp_wait_ladder.py``).
    """

    def __init__(self, message: str, *, reason: str, details: dict[str, Any]) -> None:
        self.reason = reason
        ToolError.__init__(self, message, details=details)


def _surface_backstop(reason: str, message: str, **fields: Any) -> None:
    """Log + stream_audit a backstop firing — ALWAYS visible, never opt-in-only.

    F4: the previous version only reached the opt-in ``debug.stream_audit_log``
    JSONL (a no-op unless configured — "loud" only into a file nobody reads by
    default). ``logger.warning`` fires unconditionally now; ``stream_audit``
    keeps carrying the structured detail for the cases where it IS configured.
    """

    logger.warning("mcp wait backstop fired reason=%s %s: %s", reason, fields, message)
    stream_audit(reason, message=message, **fields)


def typed_call_timeout_error(tool: str, timeout: float) -> MCPCallTimeoutBackstopError:
    """Build + surface the typed ``call_timeout_s`` backstop error (#1282 D3/F4).

    Call at the site the backstop actually fires
    (``tools/mcp_executor.py::call_tool_result``, via
    :func:`run_with_activity_backstop`) instead of raising a bare
    ``TimeoutError``.
    """

    message = f"MCP tool {tool!r} timed out after {timeout:g}s with no observed activity"
    _surface_backstop(MCP_CALL_TIMEOUT_BACKSTOP, message, tool=tool, timeout_s=timeout)
    return MCPCallTimeoutBackstopError(
        message,
        reason=MCP_CALL_TIMEOUT_BACKSTOP,
        details={"reason": MCP_CALL_TIMEOUT_BACKSTOP, "tool": tool, "timeout_s": timeout},
    )


def typed_task_drive_timeout_error(task_id: str, timeout: float) -> MCPCallTimeoutBackstopError:
    """Build + surface the typed task-drive deadline backstop error (#1282 F3b).

    Call at ``tools/mcp_tasks.py::_poll_until_terminal``'s deadline check
    instead of a bare ``TimeoutError``. See the module docstring for why this
    stays typed rather than becoming unbounded: no clio call site produces a
    non-``None`` ``timeout_seconds`` today, so this is a defensive backstop
    for whatever DOES (relay's own optional pass-through), not a live path.
    """

    message = f"task {task_id} did not finish within {timeout:g}s"
    _surface_backstop(MCP_TASK_DRIVE_TIMEOUT_BACKSTOP, message, task_id=task_id, timeout_s=timeout)
    return MCPCallTimeoutBackstopError(
        message,
        reason=MCP_TASK_DRIVE_TIMEOUT_BACKSTOP,
        details={
            "reason": MCP_TASK_DRIVE_TIMEOUT_BACKSTOP,
            "task_id": task_id,
            "timeout_s": timeout,
        },
    )


# --------------------------------------------------------------------------- #
# F3a: the activity-driven backstop                                           #
# --------------------------------------------------------------------------- #


@dataclass
class ActivityClock:
    """A mutable "last seen alive" monotonic timestamp for one in-flight call.

    :meth:`touch` is called by the executor's ``progress_handler`` (a plain
    call's progress notifications) and, via :func:`touch_active_activity_clock`,
    by :func:`default_task_wait_observer` (a task-mode drive's status-transition
    polls) — the two observable "this call is alive" signals the six wait
    constraints name (progress notifications; task status transitions).
    """

    last_touch_monotonic: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_touch_monotonic = time.monotonic()


#: The currently-active call's ActivityClock, if any (#1282 F3a). Set by
#: :func:`run_with_activity_backstop` for the duration of ONE call; read by
#: :func:`touch_active_activity_clock`, called from deep inside that SAME
#: call's task-mode poll loop (``default_task_wait_observer``) with no direct
#: parameter path back to the executor's local ``ActivityClock``. A fresh
#: ``asyncio.Task`` captures a COPY of the current context at creation
#: (``contextvars`` semantics), so this is visible to code running inside the
#: task created below, on the SAME running event loop — never across an
#: ``asyncio.run()``-started NEW loop (see reactv2.py's F2 docstring for why
#: that boundary is different and unsafe for a similar contextvar).
_ACTIVE_ACTIVITY_CLOCK: "contextvars.ContextVar[ActivityClock | None]" = contextvars.ContextVar(
    "clio_mcp_active_activity_clock", default=None
)

#: How often :func:`run_with_activity_backstop` re-checks the activity-driven
#: deadline. Bounds the worst-case lateness of a backstop firing after the
#: LAST activity, never the call's own duration.
_ACTIVITY_CHECK_INTERVAL_S = 1.0


def touch_active_activity_clock() -> None:
    """Touch the currently-active call's :class:`ActivityClock`, if one is set.

    A no-op when no call has one active (a call made outside
    :func:`run_with_activity_backstop`, or no drive currently observing this
    context) — callers never need to check first.
    """

    clock = _ACTIVE_ACTIVITY_CLOCK.get()
    if clock is not None:
        clock.touch()


def activity_progress_handler(activity: "ActivityClock") -> Callable[..., Awaitable[None]]:
    """Build a ``fastmcp`` ``progress_handler`` that touches ``activity`` (#1282 F3a).

    Wired onto the executor's ``client.call_tool(..., progress_handler=...)``
    so a PLAIN call's progress notifications reset the activity-driven
    deadline. A task-mode drive's progress rides a DIFFERENT channel (proven
    by C1-S0: ``ctx.report_progress`` inside a task tool never reaches a plain
    call's progress handler) — that path resets via
    :func:`default_task_wait_observer` instead.
    """

    async def _on_progress(progress: float, total: float | None, message: str | None) -> None:
        activity.touch()

    return _on_progress


async def run_with_activity_backstop(
    coro: "Awaitable[_T]",
    *,
    tool: str,
    timeout: float,
    activity: "ActivityClock",
) -> _T:
    """Run ``coro``, enforcing an ACTIVITY-DRIVEN deadline (#1282 F3a).

    Unlike ``asyncio.wait_for(coro, timeout=timeout)`` (a flat clock over the
    WHOLE call, including a transparently-driven task's full multi-poll
    drive), the deadline here is ``activity.last_touch_monotonic + timeout`` —
    re-checked every :data:`_ACTIVITY_CHECK_INTERVAL_S` and pushed out every
    time :meth:`ActivityClock.touch` fires. A call that keeps reporting
    progress (or, for a task-mode drive, keeps transitioning status on poll)
    NEVER dies on this backstop, no matter how long it legitimately runs; a
    call that goes genuinely silent for ``timeout`` seconds still hits the
    SAME typed :class:`MCPCallTimeoutBackstopError` a flat wait_for would have
    raised, at worst :data:`_ACTIVITY_CHECK_INTERVAL_S` late.

    Cancellation-transparent: a cancellation of the AWAITING coroutine (the
    P1.6 foreground-cancellation machinery cancelling the outer future this
    call is running under) propagates by cancelling the inner task and
    re-raising, exactly like a bare ``await coro`` would.
    """

    task: "asyncio.Task[_T]" = asyncio.ensure_future(coro)
    token = _ACTIVE_ACTIVITY_CLOCK.set(activity)
    try:
        while True:
            remaining = activity.last_touch_monotonic + timeout - time.monotonic()
            if remaining <= 0:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise typed_call_timeout_error(tool, timeout)
            done, _pending = await asyncio.wait(
                {task}, timeout=min(remaining, _ACTIVITY_CHECK_INTERVAL_S)
            )
            if task in done:
                return task.result()
    except BaseException:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        raise
    finally:
        _ACTIVE_ACTIVITY_CLOCK.reset(token)


# --------------------------------------------------------------------------- #
# F1: throttled, transient wait surfacing                                     #
# --------------------------------------------------------------------------- #

#: Minimum interval between two SURFACED (emitted) wait events for the SAME
#: unchanged status, on ONE drive (#1282 F1). A status CHANGE always emits
#: immediately regardless of this window.
_WAIT_SURFACE_MIN_INTERVAL_S = 1.0


def default_task_wait_observer(key: "TaskKey") -> "OnPollHook":
    """Build the generic, per-drive wait-surfacing ``on_poll`` hook (#1282 D3/F1/F3a).

    Returns a fresh hook (its own attempt counter + throttle state) per call —
    one per drive, matching :data:`~clio_agent.tools.task_observers.
    TaskObserverFactory`'s contract of being invoked once at drive start. The
    returned hook itself NEVER raises (mirrors ``on_poll``'s existing
    contract, ``mcp_tasks.py``'s own docstring): a broken listener downstream
    is caught and logged, never left to break the drive it was only meant to
    observe.
    """

    attempt = 0
    last_status: str | None = None
    last_emitted_monotonic: float | None = None

    async def _hook(
        current: "ClientGetTaskResult", key: "TaskKey", store: "TaskRecordStore"
    ) -> None:
        nonlocal attempt, last_status, last_emitted_monotonic
        from clio_agent.tools.mcp_task_records import TERMINAL_TASK_STATES, task_wait_listener

        if current.status in TERMINAL_TASK_STATES:
            return
        attempt += 1
        # F3a: a poll observing the task is ALIVE, whether or not this
        # particular poll goes on to be SURFACED below.
        touch_active_activity_clock()

        now = time.monotonic()
        changed = current.status != last_status
        throttled = (
            last_emitted_monotonic is not None
            and (now - last_emitted_monotonic) < _WAIT_SURFACE_MIN_INTERVAL_S
        )
        if not changed and throttled:
            return
        last_status = current.status
        last_emitted_monotonic = now

        listener = task_wait_listener()
        if listener is None:
            return
        try:
            listener(key, current.status, attempt, current.poll_interval_ms)
        except Exception as exc:  # noqa: BLE001 - a broken listener must never break the wait
            logger.warning(
                "mcp task wait listener failed reason=mcp_task_wait_listener_failed "
                "task_id=%s attempt=%d: %r",
                key.task_id,
                attempt,
                exc,
            )

    return _hook
