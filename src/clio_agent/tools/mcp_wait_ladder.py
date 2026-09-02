"""MCP wait semantics: typed backstops + surfaced waits (#1282, campaign C1-S2 D3).

The owner module for the wait-side of the slice (D1/D2 own refusal semantics —
``tools/mcp_errors.py`` / ``gact/agents/reactv2.py``). Copies the SHAPE of
``arc/rpc_liveness.py``'s per-RPC stall ladder — typed per-attempt structured
surfacing, never a bare exception — with the ONE deliberate divergence the
slice spec calls out: **no terminal give-up on slowness**. ``arc/rpc_liveness``
itself is untouched (ARC's ladder is out of scope, #1282's own constraint).

Two pieces:

1. :func:`typed_call_timeout_error` — types the ``tools.mcp.call_timeout_s``
   runaway backstop's firing (``tools/mcp_executor.py``'s ``asyncio.wait_for``
   ceiling). This backstop is EXPLICITLY SANCTIONED to remain a hard, terminal
   bound (execution.py's own #1186/#1244 comment: "a RUNAWAY BACKSTOP, not an
   operational clock" — the precise clocks are a tool's #1230 declared budget,
   a caller's explicit arg, or a #1225 ``wait_for_terminal`` commitment, all of
   which already bypass it). #1282's job is narrower than eliminating it: type
   its firing and surface it (``stream_audit``) so a bare, unattributable
   ``TimeoutError`` never reaches an operator — never to make it retry forever
   (that would let ONE misbehaving call starve the executor's single
   ``_call_lock`` indefinitely, a regression the six wait constraints do not
   ask for: they bind *waiting on the SERVER*, not *how long clio itself will
   hold a shared per-executor lock for one caller*).
2. :func:`default_task_wait_observer` — the per-drive ``on_poll`` hook every
   NON-relay backend was missing entirely (``tools/task_observers.py`` only
   ever had relay's console-tail factory registered; every other task-capable
   declared server drove ``drive_task_to_terminal`` with ``on_poll=None`` —
   rule 5, "every wait names what it waits on," was silently unmet for them).
   Fires the lean, delta-styled ``mcp_task.wait`` event
   (:mod:`clio_agent.gact.mcp_task_events`'s house style, mirroring
   ``mcp_task.console``) on every OBSERVED non-terminal poll: what it waits on
   (the task/tool), the attempt number, and the server-advertised next-poll
   interval — never a duration cap, never a terminal verdict on slowness.
   ``tools/task_observers.py::resolve_task_observer`` falls back to this when
   no backend-specific factory is registered for a drive's ``server_id``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastmcp_tasks.client_models import ClientGetTaskResult

    from clio_agent.tools.mcp_task_records import TaskKey, TaskRecordStore
    from clio_agent.tools.mcp_tasks import OnPollHook

logger = logging.getLogger(__name__)

__all__ = [
    "MCP_CALL_TIMEOUT_BACKSTOP",
    "MCPCallTimeoutBackstopError",
    "default_task_wait_observer",
    "typed_call_timeout_error",
]

#: #1282 D3: the ``tools.mcp.call_timeout_s`` runaway backstop fired. A
#: documented ceiling of last resort, not a wait-ladder verdict on slowness —
#: see the module docstring.
MCP_CALL_TIMEOUT_BACKSTOP = "mcp_call_timeout_backstop"


class MCPCallTimeoutBackstopError(TimeoutError):
    """The ``tools.mcp.call_timeout_s`` runaway backstop fired (#1282 D3).

    Stays a :class:`TimeoutError` subclass on purpose: the executor's
    existing retry-safety / uncertain-mutating-timeout classification
    (``tools/execution.py``'s ``isinstance(exc, TimeoutError)`` checks) reads
    the exception TYPE, and this must keep satisfying it byte-identically —
    only its typed ``reason``/``tool``/``timeout`` attributes and the
    :func:`stream_audit` call at construction are new.
    """

    def __init__(self, tool: str, timeout: float) -> None:
        self.reason = MCP_CALL_TIMEOUT_BACKSTOP
        self.tool = tool
        self.timeout = timeout
        super().__init__(f"MCP tool {tool!r} timed out after {timeout:g}s")


def typed_call_timeout_error(tool: str, timeout: float) -> MCPCallTimeoutBackstopError:
    """Build + surface the typed call-timeout backstop error (#1282 D3).

    Call at the site the backstop actually fires
    (``tools/mcp_executor.py::call_tool_result``) instead of raising a bare
    ``TimeoutError`` — the reason reaches ``stream_audit`` unconditionally
    (best-effort/no-op unless configured, matching every other typed-reason
    call site in this codebase) so the firing is never silent.
    """

    stream_audit(MCP_CALL_TIMEOUT_BACKSTOP, tool=tool, timeout_s=timeout)
    return MCPCallTimeoutBackstopError(tool, timeout)


def default_task_wait_observer(key: "TaskKey") -> "OnPollHook":
    """Build the generic, per-drive wait-surfacing ``on_poll`` hook (#1282 D3).

    Returns a fresh hook (its own attempt counter) per call — one per drive,
    matching :data:`~clio_agent.tools.task_observers.TaskObserverFactory`'s
    contract of being invoked once at drive start. The returned hook itself
    NEVER raises (mirrors ``on_poll``'s existing contract, ``mcp_tasks.py``'s
    own docstring): a broken listener downstream is caught and logged, never
    left to break the drive it was only meant to observe.
    """

    attempt = 0

    async def _hook(
        current: "ClientGetTaskResult", key: "TaskKey", store: "TaskRecordStore"
    ) -> None:
        nonlocal attempt
        from clio_agent.tools.mcp_task_records import TERMINAL_TASK_STATES, task_wait_listener

        if current.status in TERMINAL_TASK_STATES:
            return
        attempt += 1
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
