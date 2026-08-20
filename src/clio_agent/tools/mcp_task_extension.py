"""The SEP-2663 tasks CLIENT EXTENSION and its per-client declaration (#1115).

Split from :mod:`clio_agent.tools.mcp_tasks`, which owns the protocol operations
(the ``Mcp-Name``-bearing task RPCs, the poll loop, cancel, resume). THIS module
owns the seam where those operations attach to a FastMCP ``Client``: the backend
identity a task is recorded under, the ``ResultClaim`` resolver that turns a claimed
``CreateTaskResult`` into the tool's real result, and the decision of whether a given
client declares the extension at all.

The dependency runs one way — this module imports the operations, never the reverse —
so :func:`clio_agent.tools.mcp_runtime.make_mcp_client` imports
:func:`tasks_declaration` from here directly.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import mcp_types
from fastmcp_tasks.client import (
    TasksClientExtension,
    _inlined_call_tool_result,
    _terminal_error_message,
)

from clio_agent.errors import (
    MCP_TASK_RECORD_NOT_DURABLE,
    MCP_TASKS_DECLARATION_SUPPRESSED,
    ToolError,
)
from clio_agent.tools.mcp_task_records import (
    TaskKey,
    TaskRecord,
    TaskRecordStore,
    resolve_task_session_id,
    task_record_store,
)
from clio_agent.tools.mcp_tasks import (
    drive_task_to_terminal,
    send_task_cancel,
    session_elicitation_callback,
    utcnow_iso,
)
from clio_agent.tools.task_observers import resolve_task_observer

logger = logging.getLogger(__name__)

__all__ = [
    "BackendIdentity",
    "ClioTasksClientExtension",
    "TasksDeclaration",
    "backend_identity",
    "persist_created_task",
    "tasks_declaration",
]

# --------------------------------------------------------------------------- #
# Backend identity: a task id is only unique WITHIN one server                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BackendIdentity:
    """A stable id for one MCP backend, plus the locator needed to reconnect to it.

    ``task_id`` is minted by the server, so it is unique only within that server; two
    backends can hand out the same string. Every durable record therefore carries
    this identity, and :class:`~clio_agent.tools.mcp_task_records.TaskKey` keys on
    ``server_id``. ``locator`` is the transport-shaped description a future sweep
    needs to rebuild a client for the backend (url, or command + args).
    """

    server_id: str
    locator: dict[str, Any]


def _locator_for(target: Any) -> dict[str, Any]:
    """Describe ``target`` in the terms needed to reconnect to it later."""

    url = getattr(target, "url", None)
    if isinstance(url, str) and url:
        return {"transport": "http", "url": url}
    command = getattr(target, "command", None)
    if isinstance(command, str) and command:
        args = getattr(target, "args", None)
        return {
            "transport": "stdio",
            "command": command,
            "args": [str(a) for a in args] if isinstance(args, Sequence) else [],
        }
    name = getattr(target, "name", None)
    return {
        "transport": "in_process",
        "type": type(target).__name__,
        "name": str(name) if isinstance(name, str) else "",
    }


def backend_identity(target: Any) -> BackendIdentity:
    """Derive the stable :class:`BackendIdentity` for one client's target.

    The digest is over the reconnect locator, so the same declared server yields the
    same ``server_id`` across restarts (which is what makes a persisted record
    findable) while two different backends never collide.
    """

    locator = _locator_for(target)
    canonical = json.dumps(locator, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return BackendIdentity(server_id=digest, locator=locator)


async def persist_created_task(
    session: Any,
    key: TaskKey,
    create_result: Any,
    *,
    identity: BackendIdentity,
    tool_name: str = "",
    store: TaskRecordStore | None = None,
) -> None:
    """Make a newly accepted task durable before any task-management RPC.

    This is the shared create-to-record seam used by both the transparent #1115
    extension and the relay handle-first wrapper.  Keeping it here preserves the
    same typed orphan failure and best-effort ``tasks/cancel`` behavior on both
    surfaces instead of letting the relay path grow a second durability policy.
    """

    record_store = store if store is not None else task_record_store()
    record = TaskRecord(
        key=key,
        tool=tool_name,
        backend=dict(identity.locator),
        status=create_result.status,
        created_at=getattr(create_result, "created_at", "") or utcnow_iso(),
    )
    try:
        record_store.put(record)
    except Exception as exc:  # noqa: BLE001 - converted to the typed recovery error below
        cancelled = await _best_effort_cancel(session, key)
        logger.error(
            "mcp task %s could not be made durable reason=%s server=%s cancel_attempted=%s: %s",
            key.task_id,
            MCP_TASK_RECORD_NOT_DURABLE,
            key.server_id,
            cancelled,
            exc,
        )
        raise ToolError(
            f"task {key.task_id} was created on the server but its id could not be "
            "persisted; it cannot be resumed after a restart",
            details={
                "reason": MCP_TASK_RECORD_NOT_DURABLE,
                **key.to_wire(),
                "backend": dict(identity.locator),
                "cancel_attempted": cancelled,
                "persist_error": str(exc),
            },
        ) from exc


async def _best_effort_cancel(session: Any, key: TaskKey) -> bool:
    """Try to stop an unrecorded task and report whether its ack landed."""

    try:
        await send_task_cancel(session, key.task_id)
    except Exception as exc:  # noqa: BLE001 - best effort is reported, never hidden
        logger.warning("best-effort tasks/cancel for orphaned task %s failed: %s", key.task_id, exc)
        return False
    return True


# --------------------------------------------------------------------------- #
# The extension                                                               #
# --------------------------------------------------------------------------- #


class ClioTasksClientExtension(TasksClientExtension):
    """CLIO's tasks extension: the substrate's declaration, CLIO's resolution.

    The capability advertisement, the ``ResultClaim`` and its model all stay the
    substrate's (subclassing keeps the identifier ``io.modelcontextprotocol/tasks``,
    which is what makes a client fold THIS extension in place of the internal one).
    Only the resolution of a claimed ``CreateTaskResult`` is overridden, so the id is
    persisted under its composite identity before polling, and the drive uses the
    payload-bearing ledger, the exclusive lease, and the ``Mcp-Name``-bearing RPCs.
    """

    def __init__(self, identity: BackendIdentity, tool_name: str = "") -> None:
        # The substrate stores a construction-time elicitation callback; CLIO
        # deliberately passes NONE and resolves it from the live ``ClientSession``
        # instead. fastmcp wraps the caller's 4-argument ``elicitation_handler`` into
        # the SDK's 2-argument ``ElicitationFnT`` and installs THAT on the session,
        # so the object handed to an extension factory has the wrong signature for a
        # task's input round — calling it raises ``TypeError`` mid-task. Reading the
        # callback off the session is what makes in-task input land on the SAME HITL
        # surface a foreground elicitation lands on.
        super().__init__(None)
        self._clio_identity = identity
        self._clio_tool_name = tool_name

    @property
    def backend(self) -> BackendIdentity:
        """The identity of the backend this extension's client talks to."""

        return self._clio_identity

    async def _resolve_task(self, create_result: Any, ctx: Any) -> mcp_types.CallToolResult:
        """Persist the task id, drive it to terminal, and return the real result."""

        key = TaskKey(
            server_id=self._clio_identity.server_id,
            session_id=resolve_task_session_id(getattr(ctx, "session", None)),
            task_id=create_result.task_id,
        )
        store = task_record_store()
        # FIRST thing after the server minted the task: make the id durable. The
        # window before this line is the residual documented in the module docstring.
        await self._make_durable(ctx, key, create_result, store)
        # #1231: the transparent auto-claim path drives EVERY task-returning call
        # through here, so a per-backend observer (e.g. relay's console-tail fold,
        # registered by relay_transport.py against this task's server_id) must be
        # resolved on this path too -- the explicit relay_wait/poll path already
        # passes its hook by hand; this was the missing half. Generic on purpose:
        # this module knows nothing about relay or any other backend, only that
        # tools/task_observers.py may have one registered for this server_id.
        final = await drive_task_to_terminal(
            ctx.session,
            key,
            session_elicitation_callback(ctx.session),
            timeout_seconds=ctx.read_timeout_seconds,
            store=store,
            on_poll=resolve_task_observer(key),
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

    async def _make_durable(
        self, ctx: Any, key: TaskKey, create_result: Any, store: TaskRecordStore
    ) -> None:
        """Persist the new task, or fail LOUDLY with everything needed to reconcile.

        A task that exists on the server but nowhere locally is unrecoverable — there
        is no ``tasks/list`` to rediscover it. So a persistence failure is never
        swallowed: CLIO attempts a best-effort ``tasks/cancel`` (the only lever a
        client has to stop an orphan) and raises a typed
        ``mcp_task_record_not_durable`` error carrying the task id and the backend
        locator, so an operator can reconcile by hand.
        """

        await persist_created_task(
            ctx.session,
            key,
            create_result,
            identity=self._clio_identity,
            tool_name=self._clio_tool_name,
            store=store,
        )

    @staticmethod
    async def _best_effort_cancel(ctx: Any, key: TaskKey) -> bool:
        """Try to stop an orphaned task; report whether the ack landed."""

        return await _best_effort_cancel(ctx.session, key)


@dataclass(frozen=True)
class TasksDeclaration:
    """The tasks-extension declaration decision for one client construction."""

    extensions: tuple[Any, ...]
    reason: str | None


def tasks_declaration(client_cls: Any, target: Any = None) -> TasksDeclaration:
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

    ``target`` is the client's transport; its :func:`backend_identity` is bound onto
    the extension so every task minted through this client is recorded under the
    backend that owns it.
    """

    if not getattr(client_cls, "_auto_internal_extensions", True):
        logger.debug(
            "mcp tasks extension not declared reason=%s client_cls=%s",
            MCP_TASKS_DECLARATION_SUPPRESSED,
            getattr(client_cls, "__name__", client_cls),
        )
        return TasksDeclaration(extensions=(), reason=MCP_TASKS_DECLARATION_SUPPRESSED)
    identity = backend_identity(target)
    return TasksDeclaration(extensions=(ClioTasksClientExtension(identity),), reason=None)
