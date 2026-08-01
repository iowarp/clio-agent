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

1. **Input-key dedup that is retry- and concurrency-safe.**
   ``fastmcp_tasks.client._answer_input_requests`` re-answers the *whole*
   server-reported ``inputRequests`` map on every poll, so a poll racing an update
   prompts the human twice for one question. CLIO drives every round through
   :class:`~clio_agent.tools.mcp_task_records.TaskInputLedger`: the human's answer
   PAYLOAD is persisted before ``tasks/update`` is sent, a lost acknowledgement or a
   rejected update re-sends the identical payload without re-eliciting, and a
   :class:`~clio_agent.tools.mcp_task_records.TaskLease` keeps a second driver from
   polling or answering the same task at all.
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
4. **Durable, composite-keyed persistence + reconnect.** Nothing in the SDK survives
   losing the client. A record is keyed by ``(server_id, session_id, task_id)`` —
   never the server-minted ``task_id`` alone — and carries the reconnectable backend
   locator, so :func:`resume_task` picks up exactly the intended task on a fresh
   session. Per RULE 4 the durable home is an EXISTING store, gact session metadata;
   see :mod:`clio_agent.gact.mcp_task_store`.

RESIDUAL CRASH WINDOW (bounded by design, pending relay-side work). The server mints
the task before this client can persist anything, so there is an irreducible window
between the ``CreateTaskResult`` arriving and ``store.put`` returning. CLIO shrinks
it to the minimum — ``put`` is the FIRST thing the resolver does — and refuses to
lose the task quietly if it fails: :func:`ClioTasksClientExtension._resolve_task`
attempts a best-effort ``tasks/cancel`` and raises a typed
``mcp_task_record_not_durable`` error carrying the taskId and the backend identity,
so an operator can reconcile the orphan by hand. Closing the window properly needs a
server-supported idempotency token or an atomic create-and-record operation; that is
relay-side work in the P2 transport slice, not something a client can synthesize.
SEP-2663 has no ``tasks/list``, so an orphan created inside this window cannot be
rediscovered after a restart — which is exactly why the failure is loud.

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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import mcp_types
from fastmcp.utilities.tasks import TASKS_EXTENSION_ID
from fastmcp_tasks.client import MIN_POLL_INTERVAL, _next_poll_delay
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

from clio_agent.errors import MCP_TASK_INPUT_NO_PROGRESS, ToolError
from clio_agent.tools.mcp_task_records import (
    TERMINAL_TASK_STATES,
    InMemoryTaskRecordStore,
    TaskInputAnswer,
    TaskInputLedger,
    TaskKey,
    TaskLease,
    TaskRecord,
    TaskRecordStore,
    iter_task_records,
    open_task_records,
    persist_ledger,
    resolve_store,
    set_task_canceller,
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
    "InMemoryTaskRecordStore",
    "NamedCancelTaskRequest",
    "NamedGetTaskRequest",
    "NamedUpdateTaskRequest",
    "TaskInputAnswer",
    "TaskInputLedger",
    "TaskKey",
    "TaskLease",
    "TaskRecord",
    "TaskRecordStore",
    "cancel_task",
    "drive_task_to_terminal",
    "iter_task_records",
    "open_task_records",
    "resume_task",
    "send_task_cancel",
    "send_task_get",
    "send_task_update",
    "session_elicitation_callback",
    "utcnow_iso",
    "set_task_canceller",
    "set_task_record_store",
    "set_task_session_resolver",
    "task_record_store",
    "task_record_store_is_durable",
]

#: Wire params key carrying the task id on every task RPC — the value the SDK
#: session mirrors into the ``Mcp-Name`` header via ``Request.name_param``.
TASK_ID_PARAM = "taskId"

#: SEP-2663 Final has no ``tasks/list`` and no ``tasks/result``. Recorded so the
#: removal is assertable (``tests/test_tools/test_mcp_tasks.py``) rather than a
#: comment nobody can check.
REMOVED_TASK_METHODS: tuple[str, ...] = ("tasks/list", "tasks/result")


def utcnow_iso() -> str:
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


def session_elicitation_callback(session: Any) -> Any:
    """The SDK-shaped ``(context, params)`` elicitation callback the session will use.

    THE ONE HITL SURFACE, resolved from the object that owns it. A fastmcp ``Client``
    wraps the caller's 4-argument ``elicitation_handler`` into the SDK's 2-argument
    ``ElicitationFnT`` and installs THAT on the ``ClientSession``; the raw handler
    passed at construction has the wrong signature for a task's in-task input round.
    Reading it off the session is therefore not a convenience — it is the only way a
    task's question reaches the same handler a foreground elicitation reaches.

    The SDK's own decline-everything default is filtered out to ``None`` so a client
    with no HITL surface raises the typed "no elicitation handler" error instead of
    silently declining every in-task question.
    """

    from mcp.client.session import _default_elicitation_callback  # noqa: PLC0415

    callback = getattr(session, "_elicitation_callback", None)
    return None if callback is _default_elicitation_callback else callback


async def _elicit_answer(
    session: "ClientSession",
    task_id: str,
    key: str,
    payload: Any,
    elicitation_callback: Any,
    budget: float | None,
) -> dict[str, Any]:
    """Ask the human ONE input key's question and return the wire payload."""

    method = payload.get("method") if isinstance(payload, Mapping) else None
    if method != "elicitation/create":
        raise ToolError(
            f"task {task_id} requested in-task input via {method!r}; only elicitation "
            "is answerable on the modern protocol",
            details={"task_id": task_id, "input_key": key, "method": method},
        )
    request = mcp_types.ElicitRequest.model_validate(payload)
    context = ClientRequestContext(session=session, request_id=f"task-{task_id}-{key}")
    call = elicitation_callback(context, request.params)
    answer = await (asyncio.wait_for(call, budget) if budget is not None else call)
    if isinstance(answer, mcp_types.ErrorData):
        raise ToolError(
            f"elicitation for task {task_id} failed: {answer.message}",
            details={"task_id": task_id, "input_key": key},
        )
    return answer.model_dump(by_alias=True, mode="json", exclude_none=True)


async def _answer_round(
    session: "ClientSession",
    key: TaskKey,
    input_requests: Mapping[str, Any],
    ledger: TaskInputLedger,
    store: TaskRecordStore,
    elicitation_callback: Any,
    read_timeout_seconds: float | None,
) -> list[str]:
    """Answer one ``input_required`` round and return the keys newly elicited.

    The round is deliberately built from the SERVER's outstanding key set, not from
    what the ledger thinks is unanswered:

    * a key with no stored answer is elicited ONCE, and its payload is persisted
      BEFORE anything is transmitted;
    * a key that already has a stored answer — captured but unacknowledged, or even
      marked delivered while the server keeps reporting it — is retransmitted with
      the IDENTICAL payload and never re-asked.

    So a lost ``tasks/update`` response, an update the server rejects, and
    ledger/server divergence all converge on the same safe behavior: retry the same
    bytes. The drive's no-progress bound is what stops an endless retry loop.
    """

    outstanding = list(input_requests.keys())
    newly_elicited: list[str] = []
    if elicitation_callback is None and ledger.unelicited(outstanding):
        raise ToolError(
            f"task {key.task_id} requires input but this MCP client has no elicitation "
            "handler; in-task input cannot reach the HITL surface",
            details={"task_id": key.task_id, "input_keys": ledger.unelicited(outstanding)},
        )

    loop = asyncio.get_event_loop()
    deadline = None if read_timeout_seconds is None else loop.time() + read_timeout_seconds

    def remaining() -> float | None:
        if deadline is None:
            return None
        left = deadline - loop.time()
        if left <= 0:
            raise TimeoutError(f"task {key.task_id} timed out awaiting input")
        return left

    for input_key in outstanding:
        if ledger.answer(input_key) is not None:
            continue
        payload = await _elicit_answer(
            session,
            key.task_id,
            input_key,
            input_requests[input_key],
            elicitation_callback,
            remaining(),
        )
        ledger.capture(input_key, payload)
        # DURABLE BEFORE TRANSMITTED: a crash here still leaves an answer that a
        # resume replays verbatim instead of re-asking the human.
        persist_ledger(store, key, ledger)
        newly_elicited.append(input_key)

    responses = ledger.payloads_for(outstanding)
    if responses:
        await send_task_update(session, key.task_id, responses, remaining())
        ledger.mark_delivered(list(responses))
        persist_ledger(store, key, ledger)
    return newly_elicited


async def drive_task_to_terminal(
    session: "ClientSession",
    key: TaskKey,
    elicitation_callback: Any = None,
    *,
    timeout_seconds: float | None = None,
    ledger: TaskInputLedger | None = None,
    store: TaskRecordStore | None = None,
    max_no_progress_rounds: int | None = None,
    lease: TaskLease | None = None,
    poll_sleep: Callable[[float], Awaitable[None]] | None = None,
) -> ClientGetTaskResult:
    """Poll ``tasks/get`` to a terminal state under an exclusive lease.

    ``working`` sleeps for the server-advertised ``pollIntervalMs`` cadence (the
    substrate's ramp, honored exactly) and polls again. ``input_required`` runs
    :func:`_answer_round`, which elicits each key at most once and retransmits stored
    payloads verbatim otherwise. A round that elicits NO new key is either a
    legitimate race or a server/ledger divergence; both are retried, and after
    ``max_no_progress_rounds`` such rounds the drive raises the typed
    ``mcp_task_input_no_progress`` error rather than spinning forever. The bound
    defaults to the #1114 ``tools.mcp.input_required_max_rounds`` config value.

    A :class:`~clio_agent.tools.mcp_task_records.TaskLease` is taken for the whole
    drive, so a second concurrent driver of the same task is refused with
    ``mcp_task_lease_held`` instead of double-polling and double-answering. Pass
    ``lease`` to reuse a lease the caller already holds.

    ``poll_sleep`` is the poll loop's clock boundary. Tests may inject a recorder
    without replacing the process-wide event-loop scheduler; production uses
    :func:`asyncio.sleep`.

    The record's status is written through ``store`` on every observed transition and
    dropped once the task settles, so a crash leaves behind exactly the ids that are
    still live.
    """

    record_store = resolve_store(store)
    if ledger is None:
        ledger = TaskInputLedger.from_record(record_store.get(key))
    if elicitation_callback is None:
        elicitation_callback = session_elicitation_callback(session)
    if max_no_progress_rounds is None:
        from clio_agent.tools.mcp_runtime import input_required_max_rounds  # noqa: PLC0415

        max_no_progress_rounds = input_required_max_rounds()

    owned_lease = lease is None
    active = lease if lease is not None else TaskLease(record_store, key)
    if owned_lease:
        active.acquire()
    try:
        return await _poll_until_terminal(
            session,
            key,
            elicitation_callback,
            timeout_seconds=timeout_seconds,
            ledger=ledger,
            store=record_store,
            max_no_progress_rounds=max_no_progress_rounds,
            poll_sleep=asyncio.sleep if poll_sleep is None else poll_sleep,
        )
    finally:
        if owned_lease:
            active.release()


async def _poll_until_terminal(
    session: "ClientSession",
    key: TaskKey,
    elicitation_callback: Any,
    *,
    timeout_seconds: float | None,
    ledger: TaskInputLedger,
    store: TaskRecordStore,
    max_no_progress_rounds: int,
    poll_sleep: Callable[[float], Awaitable[None]],
) -> ClientGetTaskResult:
    """The lease-protected poll loop body (see :func:`drive_task_to_terminal`)."""

    loop = asyncio.get_event_loop()
    deadline = None if timeout_seconds is None else loop.time() + timeout_seconds
    backoff = MIN_POLL_INTERVAL
    no_progress = 0

    def remaining() -> float | None:
        return None if deadline is None else deadline - loop.time()

    while True:
        budget = remaining()
        if budget is not None and budget <= 0:
            raise TimeoutError(f"task {key.task_id} did not finish within {timeout_seconds}s")

        current = await send_task_get(session, key.task_id, budget)
        _record_status(store, ledger, key, current.status)
        if current.status in TERMINAL_TASK_STATES:
            store.drop(key)
            return current
        if current.status == "input_required":
            newly_elicited = await _answer_round(
                session,
                key,
                current.input_requests or {},
                ledger,
                store,
                elicitation_callback,
                remaining(),
            )
            _record_status(store, ledger, key, current.status)
            if newly_elicited:
                no_progress = 0
                backoff = MIN_POLL_INTERVAL
                continue
            no_progress += 1
            if no_progress > max_no_progress_rounds:
                raise ToolError(
                    f"task {key.task_id} kept reporting input_required with no new key "
                    f"across {no_progress} polls, despite retransmitting every stored "
                    "answer verbatim",
                    details={
                        "reason": MCP_TASK_INPUT_NO_PROGRESS,
                        "task_id": key.task_id,
                        "server_id": key.server_id,
                        "delivered_keys": sorted(ledger.delivered_keys()),
                        "max_no_progress_rounds": max_no_progress_rounds,
                    },
                )
        else:
            no_progress = 0
        delay, backoff = _next_poll_delay(current.poll_interval_ms, backoff)
        budget = remaining()
        if budget is not None:
            delay = min(delay, budget)
        await poll_sleep(delay)


def _record_status(
    store: TaskRecordStore, ledger: TaskInputLedger, key: TaskKey, status: str
) -> None:
    """Write an observed status (and the answer ledger) through to the store."""

    existing = store.get(key)
    if existing is None:
        return
    store.put(replace(existing, status=status, input_answers=ledger.snapshot()))


# --------------------------------------------------------------------------- #
# Cancel — ack-only, deliberately NOT the #1116 foreground path                #
# --------------------------------------------------------------------------- #


async def cancel_task(
    session: "ClientSession", key: TaskKey, *, store: TaskRecordStore | None = None
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
    emitted for the task. The record for the FULL composite key is dropped once the
    ack lands — never every row that happens to share the task id.
    """

    ack = await send_task_cancel(session, key.task_id)
    resolve_store(store).drop(key)
    return ack


# --------------------------------------------------------------------------- #
# Gap 4: reconnect-by-task-id                                                 #
# --------------------------------------------------------------------------- #


async def resume_task(
    session: "ClientSession",
    key: TaskKey,
    *,
    elicitation_callback: Any = None,
    timeout_seconds: float | None = None,
    store: TaskRecordStore | None = None,
) -> ClientGetTaskResult:
    """Resume a persisted task on a FRESH session and drive it to a terminal state.

    The crash-recovery primitive the P2 relay transport depends on: the client that
    started the task is gone, but the id is durable, so a new client picks the task
    up with ``tasks/get`` and keeps polling. Resolution takes the FULL composite key
    — resuming by bare task id could seed one backend's task with another's answers.
    The record's persisted answers seed the ledger, so a question the human answered
    before the crash is neither re-asked nor lost: an undelivered answer is
    retransmitted verbatim.

    Raises:
        ToolError: If ``key`` has no persisted record — there is nothing to resume,
            and inventing a poll loop for an unknown identity would be a guess.
    """

    record_store = resolve_store(store)
    record = record_store.get(key)
    if record is None:
        raise ToolError(
            f"cannot resume task {key.task_id}: no persisted task record for "
            f"server {key.server_id!r} / session {key.session_id!r}",
            details=key.to_wire(),
        )
    return await drive_task_to_terminal(
        session,
        key,
        elicitation_callback,
        timeout_seconds=timeout_seconds,
        ledger=TaskInputLedger.from_record(record),
        store=record_store,
    )
