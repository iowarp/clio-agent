"""SEP-2663 tasks extension client — the CLIO half (#1115).

WHAT THE SUBSTRATE ALREADY DOES. There is no ``mcp/client/experimental/tasks.py``
in the installed mcp 2.0 SDK; the tasks client lives in the pinned
**``fastmcp_tasks`` 4.0.0b1** package. It supplies: the per-request extension
declaration (``TasksClientExtension``, folded into a client's capability ad by
``Client._build_extension_kwargs``), the ``ResultClaim`` for ``resultType:
"task"`` (:class:`fastmcp_tasks.client_models.ClientCreateTaskResult`), the
``tasks/get`` poll loop with ``pollIntervalMs`` backoff, ``input_required`` rounds
dispatched through the client's elicitation callback (CLIO's #1113 handler, so
in-task input lands on the ONE HITL surface), and ack-only ``tasks/cancel``.
``tasks/list`` and ``tasks/result`` do not exist in SEP-2663 Final and nothing
here calls them.

WHAT THIS MODULE BUILDS — the four gaps the substrate leaves open:

1. **Input-key dedup.** ``fastmcp_tasks.client._answer_input_requests`` re-answers
   the *whole* server-reported ``inputRequests`` map on every poll. A key stays in
   that map until the server has processed the ``tasks/update`` carrying its
   answer, so a poll racing the update re-reports a key the client already
   answered — and the human is prompted twice for one question.
   :class:`TaskInputLedger` makes the answer exactly-once per key.
2. **``Mcp-Name: <taskId>``.** SEP-2663 requires task RPCs to mirror the task id
   into the ``Mcp-Name`` header. ``mcp.shared.inbound.NAME_BEARING_METHODS`` covers
   only ``tools/call`` / ``prompts/get`` / ``resources/read``, and the
   ``fastmcp_tasks`` request models leave the SDK's per-request-class
   ``Request.name_param`` hook unset — so the header is never sent. The
   ``Named*Request`` subclasses below set it and this module's senders use them
   (a subclass, never a mutation of the third-party model).
3. **#1112 classification.** ``resultType: "task"`` was a typed degrade while CLIO
   was a tasks-off client. It is a handled shape now — see
   :mod:`clio_agent.tools.mcp_results`.
4. **Durable task-id persistence + reconnect.** Nothing in the SDK survives losing
   the client. :class:`TaskRecord` / :class:`TaskRecordStore` persist the id and
   :func:`resume_task` picks the task back up on a fresh session. Per RULE 4 the
   durable home is an EXISTING store — gact session metadata; see
   :mod:`clio_agent.gact.mcp_task_store`.

CANCEL IS ACK-ONLY, AND THAT IS NOT #1116 — see :func:`cancel_task`.

IMPORT SCOPE. Importing this module imports ``fastmcp_tasks``, which registers an
internal client-extension factory with fastmcp core process-wide. It is therefore
imported LAZILY, from :func:`clio_agent.tools.mcp_runtime.make_mcp_client` only —
the one construction site for execution-path clients. Proxy backends are
structurally excluded: ``ProxyClient`` pins ``_auto_internal_extensions = False``
and :func:`tasks_declaration` honors that with a typed reason (#1119: CLIO's own
mounted servers keep tasks off; the relay owns durable tasks).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import mcp_types
from fastmcp.utilities.tasks import TASKS_EXTENSION_ID
from fastmcp_tasks.client import (
    MIN_POLL_INTERVAL,
    TasksClientExtension,
    _inlined_call_tool_result,
    _next_poll_delay,
    _terminal_error_message,
)
from fastmcp_tasks.client_models import (
    CancelTaskRequest,
    CancelTaskRequestParams,
    ClientGetTaskResult,
    GetTaskRequest,
    GetTaskRequestParams,
    UpdateTaskRequest,
    UpdateTaskRequestParams,
)
from mcp.client.session import ClientRequestContext

from clio_agent.errors import (
    MCP_TASK_INPUT_NO_PROGRESS,
    MCP_TASKS_DECLARATION_SUPPRESSED,
    ToolError,
)
from clio_agent.tools.mcp_task_records import (
    TERMINAL_TASK_STATES,
    InMemoryTaskRecordStore,
    TaskInputLedger,
    TaskRecord,
    TaskRecordStore,
    iter_task_records,
    open_task_records,
    resolve_store,
    resolve_task_session_id,
    set_task_record_store,
    set_task_session_resolver,
    task_record_store,
    task_record_store_is_durable,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.client.session import ClientSession

logger = logging.getLogger(__name__)

__all__ = [
    "REMOVED_TASK_METHODS",
    "TASKS_EXTENSION_ID",
    "TERMINAL_TASK_STATES",
    "ClioTasksClientExtension",
    "InMemoryTaskRecordStore",
    "NamedCancelTaskRequest",
    "NamedGetTaskRequest",
    "NamedUpdateTaskRequest",
    "TaskInputLedger",
    "TaskRecord",
    "TaskRecordStore",
    "TasksDeclaration",
    "cancel_task",
    "drive_task_to_terminal",
    "iter_task_records",
    "open_task_records",
    "resume_task",
    "send_task_cancel",
    "send_task_get",
    "send_task_update",
    "set_task_record_store",
    "set_task_session_resolver",
    "task_record_store",
    "task_record_store_is_durable",
    "tasks_declaration",
]

#: Wire params key carrying the task id on every task RPC — the value the SDK
#: session mirrors into the ``Mcp-Name`` header via ``Request.name_param``.
TASK_ID_PARAM = "taskId"

#: SEP-2663 Final has no ``tasks/list`` and no ``tasks/result``. Recorded so the
#: removal is assertable (``tests/test_tools/test_mcp_tasks.py``) rather than a
#: comment nobody can check.
REMOVED_TASK_METHODS: tuple[str, ...] = ("tasks/list", "tasks/result")


def _utcnow_iso() -> str:
    """Current UTC timestamp, ISO-8601 — the record's fallback ``created_at``."""

    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Gap 2: task requests that actually carry `Mcp-Name: <taskId>`                #
# --------------------------------------------------------------------------- #


class NamedGetTaskRequest(GetTaskRequest):
    """``tasks/get`` that stamps ``Mcp-Name: <taskId>`` (SEP-2663 MUST)."""

    name_param = TASK_ID_PARAM


class NamedUpdateTaskRequest(UpdateTaskRequest):
    """``tasks/update`` that stamps ``Mcp-Name: <taskId>`` (SEP-2663 MUST)."""

    name_param = TASK_ID_PARAM


class NamedCancelTaskRequest(CancelTaskRequest):
    """``tasks/cancel`` that stamps ``Mcp-Name: <taskId>`` (SEP-2663 MUST)."""

    name_param = TASK_ID_PARAM


async def send_task_get(
    session: "ClientSession", task_id: str, read_timeout_seconds: float | None = None
) -> ClientGetTaskResult:
    """Send ``tasks/get`` for ``task_id`` and return the typed response."""

    return await session.send_request(
        NamedGetTaskRequest(params=GetTaskRequestParams(task_id=task_id)),
        ClientGetTaskResult,
        request_read_timeout_seconds=read_timeout_seconds,
    )


async def send_task_update(
    session: "ClientSession",
    task_id: str,
    input_responses: Mapping[str, Any],
    read_timeout_seconds: float | None = None,
) -> None:
    """Send ``tasks/update`` delivering keyed answers to a parked task."""

    await session.send_request(
        NamedUpdateTaskRequest(
            params=UpdateTaskRequestParams(task_id=task_id, input_responses=dict(input_responses))
        ),
        mcp_types.Result,
        request_read_timeout_seconds=read_timeout_seconds,
    )


async def send_task_cancel(
    session: "ClientSession", task_id: str, read_timeout_seconds: float | None = None
) -> Any:
    """Send ``tasks/cancel`` and return the server's acknowledgement Result."""

    return await session.send_request(
        NamedCancelTaskRequest(params=CancelTaskRequestParams(task_id=task_id)),
        mcp_types.Result,
        request_read_timeout_seconds=read_timeout_seconds,
    )


# --------------------------------------------------------------------------- #
# The poll loop                                                               #
# --------------------------------------------------------------------------- #


def _input_request_keys(input_requests: Mapping[str, Any] | None) -> list[str]:
    """The server-minted keys of one ``input_required`` round, in wire order."""

    return list((input_requests or {}).keys())


async def _answer_new_input_keys(
    session: "ClientSession",
    task_id: str,
    input_requests: Mapping[str, Any],
    keys: Sequence[str],
    elicitation_callback: Any,
    read_timeout_seconds: float | None,
) -> None:
    """Answer exactly ``keys`` through the elicitation callback, then ``tasks/update``.

    Mirrors ``fastmcp_tasks.client._answer_input_requests`` with the two CLIO
    differences it cannot express: the caller has already filtered ``keys`` through
    the dedup ledger, and the update is sent with the ``Mcp-Name``-bearing request.
    Every answer goes through the client's elicitation callback — CLIO's #1113
    handler — so in-task input reaches the ONE HITL surface and introduces no
    parallel store.
    """

    if elicitation_callback is None:
        raise ToolError(
            f"task {task_id} requires input but this MCP client has no elicitation "
            "handler; in-task input cannot reach the HITL surface",
            details={"task_id": task_id, "input_keys": list(keys)},
        )

    loop = asyncio.get_event_loop()
    deadline = None if read_timeout_seconds is None else loop.time() + read_timeout_seconds

    def remaining() -> float | None:
        if deadline is None:
            return None
        left = deadline - loop.time()
        if left <= 0:
            raise TimeoutError(f"task {task_id} timed out awaiting input")
        return left

    responses: dict[str, Any] = {}
    for key in keys:
        payload = input_requests[key]
        method = payload.get("method") if isinstance(payload, Mapping) else None
        if method != "elicitation/create":
            raise ToolError(
                f"task {task_id} requested in-task input via {method!r}; only "
                "elicitation is answerable on the modern protocol",
                details={"task_id": task_id, "input_key": key, "method": method},
            )
        request = mcp_types.ElicitRequest.model_validate(payload)
        context = ClientRequestContext(session=session, request_id=f"task-{task_id}-{key}")
        budget = remaining()
        call = elicitation_callback(context, request.params)
        answer = await (asyncio.wait_for(call, budget) if budget is not None else call)
        if isinstance(answer, mcp_types.ErrorData):
            raise ToolError(
                f"elicitation for task {task_id} failed: {answer.message}",
                details={"task_id": task_id, "input_key": key},
            )
        responses[key] = answer.model_dump(by_alias=True, mode="json", exclude_none=True)

    await send_task_update(session, task_id, responses, remaining())


async def drive_task_to_terminal(
    session: "ClientSession",
    task_id: str,
    elicitation_callback: Any = None,
    *,
    timeout_seconds: float | None = None,
    ledger: TaskInputLedger | None = None,
    store: TaskRecordStore | None = None,
    max_no_progress_rounds: int | None = None,
) -> ClientGetTaskResult:
    """Poll ``tasks/get`` to a terminal state, answering each input key exactly once.

    ``working`` sleeps for the server-advertised ``pollIntervalMs`` cadence (the
    substrate's ramp, honored exactly) and polls again. ``input_required`` answers
    only the keys the ledger has not seen and re-enters via ``tasks/update``. A
    round that surfaces NO new key is a legitimate race — the server has not yet
    processed the update just sent — and is simply re-polled; but it cannot repeat
    forever: after ``max_no_progress_rounds`` such rounds the drive raises the typed
    ``mcp_task_input_no_progress`` error rather than spinning silently. The bound
    defaults to the #1114 ``tools.mcp.input_required_max_rounds`` config value, the
    same knob that bounds the foreground input-required loop.

    The record's status is written through ``store`` on every observed transition
    and dropped once the task settles, so a crash leaves behind exactly the ids
    that are still live.
    """

    ledger = ledger if ledger is not None else TaskInputLedger()
    record_store = resolve_store(store)
    if max_no_progress_rounds is None:
        from clio_agent.tools.mcp_runtime import input_required_max_rounds  # noqa: PLC0415

        max_no_progress_rounds = input_required_max_rounds()

    loop = asyncio.get_event_loop()
    deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
    backoff = MIN_POLL_INTERVAL
    no_progress = 0

    def remaining() -> float | None:
        return None if deadline is None else deadline - loop.time()

    while True:
        budget = remaining()
        if budget is not None and budget <= 0:
            raise TimeoutError(f"task {task_id} did not finish within {timeout_seconds}s")

        current = await send_task_get(session, task_id, budget)
        _record_status(record_store, ledger, task_id, current.status)
        if current.status in TERMINAL_TASK_STATES:
            record_store.drop(task_id)
            ledger.forget(task_id)
            return current
        if current.status == "input_required":
            requests = current.input_requests or {}
            new_keys = ledger.unanswered(task_id, _input_request_keys(requests))
            if new_keys:
                no_progress = 0
                await _answer_new_input_keys(
                    session, task_id, requests, new_keys, elicitation_callback, remaining()
                )
                ledger.mark_answered(task_id, new_keys)
                _record_status(record_store, ledger, task_id, current.status)
                backoff = MIN_POLL_INTERVAL
                continue
            no_progress += 1
            if no_progress > max_no_progress_rounds:
                raise ToolError(
                    f"task {task_id} kept reporting input_required with no unanswered "
                    f"key across {no_progress} polls",
                    details={
                        "reason": MCP_TASK_INPUT_NO_PROGRESS,
                        "task_id": task_id,
                        "answered_keys": sorted(ledger.answered(task_id)),
                        "max_no_progress_rounds": max_no_progress_rounds,
                    },
                )
        else:
            no_progress = 0
        delay, backoff = _next_poll_delay(current.poll_interval_ms, backoff)
        budget = remaining()
        if budget is not None:
            delay = min(delay, budget)
        await asyncio.sleep(delay)


def _record_status(
    store: TaskRecordStore, ledger: TaskInputLedger, task_id: str, status: str
) -> None:
    """Write an observed status (and the answered-key set) through to the store."""

    existing = store.get(task_id)
    if existing is None:
        return
    store.put(
        replace(
            existing,
            status=status,
            answered_input_keys=tuple(sorted(ledger.answered(task_id))),
        )
    )


# --------------------------------------------------------------------------- #
# Cancel — ack-only, deliberately NOT the #1116 foreground path                #
# --------------------------------------------------------------------------- #


async def cancel_task(
    session: "ClientSession", task_id: str, *, store: TaskRecordStore | None = None
) -> Any:
    """Cooperatively cancel a task by SENDING ``tasks/cancel`` and reading the ack.

    THE SPLIT FROM #1116, stated here because the two look alike and are not:

    * #1116 (:mod:`clio_agent.tools.foreground_cancellation`) cancels an in-flight
      FOREGROUND request. It cancels the ``call_tool`` coroutine, which makes MCP's
      dispatcher emit ``notifications/cancelled`` for the request id it allocated.
      The unit of cancellation there is a REQUEST.
    * This cancels a BACKGROUND TASK. The unit is a task id that outlives the
      request which created it, so there is no in-flight request to abandon and
      ``notifications/cancelled`` would name nothing. SEP-2663 cancellation is a
      request/ack: send ``tasks/cancel``, read the acknowledgement, and the task
      settles to ``cancelled`` on a later ``tasks/get``.

    Nothing here cancels a coroutine, so no ``notifications/cancelled`` frame is
    emitted for the task. The persisted record is dropped once the ack lands: the
    task is settling and must not be resumed.
    """

    ack = await send_task_cancel(session, task_id)
    resolve_store(store).drop(task_id)
    return ack


# --------------------------------------------------------------------------- #
# Gap 4: reconnect-by-task-id                                                 #
# --------------------------------------------------------------------------- #


async def resume_task(
    session: "ClientSession",
    task_id: str,
    *,
    elicitation_callback: Any = None,
    timeout_seconds: float | None = None,
    store: TaskRecordStore | None = None,
) -> ClientGetTaskResult:
    """Resume a persisted task on a FRESH session and drive it to a terminal state.

    The crash-recovery primitive the P2 relay transport depends on: the client that
    started the task is gone, but the id is durable, so a new client picks the task
    up with ``tasks/get`` and keeps polling. The record's ``answered_input_keys``
    seed the dedup ledger, so a question the human answered before the crash is not
    asked again after it.

    Raises:
        ToolError: If ``task_id`` has no persisted record — there is nothing to
            resume, and inventing a poll loop for an unknown id would be a guess.
    """

    record_store = resolve_store(store)
    record = record_store.get(task_id)
    if record is None:
        raise ToolError(
            f"cannot resume task {task_id}: no persisted task record",
            details={"task_id": task_id},
        )
    ledger = TaskInputLedger()
    ledger.mark_answered(task_id, record.answered_input_keys)
    return await drive_task_to_terminal(
        session,
        task_id,
        elicitation_callback,
        timeout_seconds=timeout_seconds,
        ledger=ledger,
        store=record_store,
    )


# --------------------------------------------------------------------------- #
# The extension                                                               #
# --------------------------------------------------------------------------- #


class ClioTasksClientExtension(TasksClientExtension):
    """CLIO's tasks extension: the substrate's declaration, CLIO's resolution.

    The capability advertisement, the ``ResultClaim`` and its model all stay the
    substrate's (subclassing keeps the identifier ``io.modelcontextprotocol/tasks``,
    which is what makes a client fold THIS extension in place of the internal one).
    Only the resolution of a claimed ``CreateTaskResult`` is overridden, so the id
    is persisted before polling and the drive uses the dedup ledger and the
    ``Mcp-Name``-bearing task RPCs.
    """

    def __init__(self, elicitation_callback: Any = None, tool_name: str = "") -> None:
        super().__init__(elicitation_callback)
        self._clio_tool_name = tool_name

    async def _resolve_task(self, create_result: Any, ctx: Any) -> mcp_types.CallToolResult:
        """Persist the task id, drive it to terminal, and return the real result."""

        task_id = create_result.task_id
        store = task_record_store()
        store.put(
            TaskRecord(
                task_id=task_id,
                tool=self._clio_tool_name,
                session_id=self._session_id_for(ctx),
                status=create_result.status,
                created_at=getattr(create_result, "created_at", "") or _utcnow_iso(),
            )
        )
        final = await drive_task_to_terminal(
            ctx.session,
            task_id,
            self._elicitation_callback,
            timeout_seconds=ctx.read_timeout_seconds,
            store=store,
        )
        if final.status == "completed":
            return _inlined_call_tool_result(final.result)
        message = (
            _terminal_error_message(final)
            if final.status == "failed"
            else f"task {final.task_id} was cancelled"
        )
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=message)], is_error=True
        )

    @staticmethod
    def _session_id_for(ctx: Any) -> str | None:
        """Resolve the CLIO session that owns this task, when a resolver is installed."""

        return resolve_task_session_id(getattr(ctx, "session", None))


@dataclass(frozen=True)
class TasksDeclaration:
    """The tasks-extension declaration decision for one client construction."""

    extensions: tuple[Any, ...]
    reason: str | None


def tasks_declaration(elicitation_callback: Any, client_cls: Any) -> TasksDeclaration:
    """Resolve whether this client declares the tasks extension, and why not if not.

    A client class that pins ``_auto_internal_extensions = False`` — FastMCP's
    ``ProxyClient`` does — is declaring that it MUST NOT advertise task support to
    its backend: a proxy relays a call synchronously and has no path to drive a
    backend task on the front connection's behalf. CLIO honors that instead of
    overriding it, and records the typed reason
    ``mcp_tasks_declaration_suppressed`` rather than returning nothing quietly.
    That suppression is also what keeps tasks OFF for CLIO's own servers (#1119):
    the built-in ``fs``/``shell`` servers install no tasks extension server-side,
    and every declared server is reached through the proxy backend.
    """

    if not getattr(client_cls, "_auto_internal_extensions", True):
        logger.debug(
            "mcp tasks extension not declared reason=%s client_cls=%s",
            MCP_TASKS_DECLARATION_SUPPRESSED,
            getattr(client_cls, "__name__", client_cls),
        )
        return TasksDeclaration(extensions=(), reason=MCP_TASKS_DECLARATION_SUPPRESSED)
    return TasksDeclaration(
        extensions=(ClioTasksClientExtension(elicitation_callback),), reason=None
    )
