"""Tasks extension client (SEP-2663, #1115).

The substrate is the **`fastmcp_tasks`** package (note: `mcp/client/experimental/tasks.py`
does NOT exist in the installed mcp 2.0 SDK). It provides the client extension, the
`tasks/get` poll loop honoring `pollIntervalMs`, `input_required` rounds via
`tasks/update` dispatched through the client's elicitation callback (CLIO's P1.3
handler, so inputs land on the ONE HITL surface), and ack-only `tasks/cancel`.

CLIO builds what the substrate lacks: per-poll input-key DEDUP, the `Mcp-Name: <taskId>`
header on task RPCs, durable task-id persistence + reconnect-by-task-id, and #1112
classification tolerance for `resultType: "task"`.

These tests drive the poll loop against a scripted fake `ClientSession` that records
every request it is sent, so the wire assertions (which method, which params, which
header the SDK would stamp) are made on the objects the SDK itself consumes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest  # noqa: F401 - pytest.raises / approx used throughout

from clio_agent.errors import (
    MCP_TASK_INPUT_NO_PROGRESS,
    MCP_TASKS_DECLARATION_SUPPRESSED,
    ToolError,
)
from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskInputLedger,
    TaskRecord,
    set_task_record_store,
)
from clio_agent.tools.mcp_tasks import (
    REMOVED_TASK_METHODS,
    ClioTasksClientExtension,
    cancel_task,
    drive_task_to_terminal,
    resume_task,
    task_record_store,
    tasks_declaration,
)

# --------------------------------------------------------------------------- #
# Scripted session double                                                     #
# --------------------------------------------------------------------------- #


def _task_payload(
    task_id: str,
    status: str,
    *,
    poll_interval_ms: float | None = None,
    result: dict[str, Any] | None = None,
    input_requests: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One `tasks/get` wire response (camelCase, as a server dumps it)."""

    payload: dict[str, Any] = {
        "taskId": task_id,
        "status": status,
        "createdAt": "2026-07-31T00:00:00+00:00",
        "lastUpdatedAt": "2026-07-31T00:00:00+00:00",
        "resultType": "complete",
    }
    if poll_interval_ms is not None:
        payload["pollIntervalMs"] = poll_interval_ms
    if result is not None:
        payload["result"] = result
    if input_requests is not None:
        payload["inputRequests"] = input_requests
    return payload


def _elicit_request(message: str) -> dict[str, Any]:
    """A serialized `elicitation/create` request as a task surfaces it."""

    return {
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": message,
            "requestedSchema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
            },
        },
    }


class ScriptedSession:
    """A fake `ClientSession` that answers `tasks/*` from a queued script.

    Records every request OBJECT it is handed, so the tests can assert on the SDK's
    own `Request.name_param` (the value the session mirrors into `Mcp-Name`) and on
    the exact params sent, rather than on a re-implementation of the wire.
    """

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = list(script)
        self.requests: list[Any] = []
        self.sleeps: list[float] = []
        self.notifications: list[Any] = []
        self.updates: list[dict[str, Any]] = []

    async def send_request(
        self,
        request: Any,
        result_type: Any,
        request_read_timeout_seconds: float | None = None,
    ) -> Any:
        """Answer one scripted task RPC."""

        self.requests.append(request)
        method = request.method
        if method == "tasks/get":
            payload = self._script.pop(0)
            return result_type.model_validate(payload)
        if method == "tasks/update":
            self.updates.append(dict(request.params.input_responses))
            return result_type()
        if method == "tasks/cancel":
            return result_type()
        raise AssertionError(f"unexpected task RPC {method!r}")

    async def send_notification(self, notification: Any) -> None:
        """Record any notification — the cancel test asserts none is sent."""

        self.notifications.append(notification)

    def methods(self) -> list[str]:
        """The ordered methods this session was asked to send."""

        return [request.method for request in self.requests]


def _fresh_store() -> InMemoryTaskRecordStore:
    """A store isolated from the process registry."""

    return InMemoryTaskRecordStore()


async def _answer_everything(context: Any, params: Any) -> Any:
    """An elicitation callback that accepts with a fixed answer (the HITL stand-in)."""

    from fastmcp.client.elicitation import ElicitResult

    return ElicitResult(action="accept", content={"answer": "yes"})


# --------------------------------------------------------------------------- #
# FAILING-FIRST: reconnect by task id                                         #
# --------------------------------------------------------------------------- #


async def test_reconnect_by_task_id_resumes_polling_to_completion() -> None:
    """FAILING-FIRST (#1115 headline): a task id survives losing the client.

    Persist the id from ``CreateTaskResult``, DROP the client (crash), reconstruct a
    fresh one, and resume polling that same task id to a terminal result — the crash
    recovery the P2 relay transport depends on.
    """

    store = _fresh_store()
    # The client that started the task persisted the id, then died mid-flight.
    store.put(TaskRecord(task_id="task-42", tool="slow_tool", status="working"))

    # A brand-new session (nothing carried over from the dead client).
    session = ScriptedSession(
        [
            _task_payload("task-42", "working", poll_interval_ms=1),
            _task_payload(
                "task-42",
                "completed",
                result={"content": [{"type": "text", "text": "done"}]},
            ),
        ]
    )

    final = await resume_task(session, "task-42", store=store)

    assert final.status == "completed"
    assert session.methods() == ["tasks/get", "tasks/get"]
    # Settled tasks are dropped: a later sweep must not try to resume them.
    assert store.get("task-42") is None


async def test_resume_without_a_persisted_record_is_a_typed_error() -> None:
    """Resuming an unknown id raises rather than inventing a poll loop."""

    session = ScriptedSession([])
    with pytest.raises(ToolError) as excinfo:
        await resume_task(session, "task-unknown", store=_fresh_store())
    assert excinfo.value.details["task_id"] == "task-unknown"
    assert session.requests == []


async def test_resume_seeds_the_dedup_ledger_from_the_persisted_record() -> None:
    """A key answered before the crash is not asked again after it."""

    store = _fresh_store()
    store.put(
        TaskRecord(
            task_id="task-7",
            status="input_required",
            answered_input_keys=("k1",),
        )
    )
    session = ScriptedSession(
        [
            # The server still reports k1 outstanding — it has not processed the
            # pre-crash update yet.
            _task_payload(
                "task-7",
                "input_required",
                poll_interval_ms=1,
                input_requests={"k1": _elicit_request("who?")},
            ),
            _task_payload("task-7", "completed", result={"content": []}),
        ]
    )
    prompts: list[Any] = []

    async def recording_callback(context: Any, params: Any) -> Any:
        prompts.append(params)
        return await _answer_everything(context, params)

    final = await resume_task(
        session, "task-7", elicitation_callback=recording_callback, store=store
    )

    assert final.status == "completed"
    assert prompts == []
    assert "tasks/update" not in session.methods()


# --------------------------------------------------------------------------- #
# Poll loop + pollIntervalMs                                                  #
# --------------------------------------------------------------------------- #


async def test_poll_loop_honors_server_poll_interval_ms(monkeypatch: Any) -> None:
    """The server-advertised ``pollIntervalMs`` caps the client's poll cadence."""

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    session = ScriptedSession(
        [
            _task_payload("task-1", "working", poll_interval_ms=250),
            _task_payload("task-1", "working", poll_interval_ms=250),
            _task_payload("task-1", "working", poll_interval_ms=250),
            _task_payload("task-1", "working", poll_interval_ms=250),
            _task_payload("task-1", "working", poll_interval_ms=250),
            _task_payload("task-1", "completed", result={"content": []}),
        ]
    )

    final = await drive_task_to_terminal(session, "task-1", store=_fresh_store())

    assert final.status == "completed"
    # The ramp starts fast so a quick task resolves promptly, then settles AT the
    # server's advertised cadence and never exceeds it.
    assert slept, "a working task must sleep between polls"
    assert max(slept) <= 0.25
    assert slept[-1] == pytest.approx(0.25)
    assert slept == sorted(slept), "the backoff must ramp monotonically to the ceiling"


async def test_create_task_result_is_driven_to_the_real_result() -> None:
    """The extension resolves a claimed ``CreateTaskResult`` into the tool's result."""

    from fastmcp_tasks.client_models import ClientCreateTaskResult

    store = _fresh_store()
    set_task_record_store(store)
    try:
        session = ScriptedSession(
            [
                _task_payload("task-9", "working", poll_interval_ms=1),
                _task_payload(
                    "task-9",
                    "completed",
                    result={"content": [{"type": "text", "text": "42"}]},
                ),
            ]
        )
        create = ClientCreateTaskResult.model_validate(
            {
                "taskId": "task-9",
                "status": "working",
                "createdAt": "2026-07-31T00:00:00+00:00",
                "lastUpdatedAt": "2026-07-31T00:00:00+00:00",
                "resultType": "task",
            }
        )

        class Ctx:
            """Minimal ``ClaimContext`` stand-in."""

            session = None
            read_timeout_seconds = None

        ctx = Ctx()
        ctx.session = session
        extension = ClioTasksClientExtension(None)
        result = await extension._resolve_task(create, ctx)

        assert result.content[0].text == "42"
        assert result.is_error in (False, None)
        # The id was persisted while in flight and dropped once terminal.
        assert store.get("task-9") is None
    finally:
        set_task_record_store(None)


# --------------------------------------------------------------------------- #
# Gap 1: input-key dedup                                                      #
# --------------------------------------------------------------------------- #


async def test_input_key_answered_exactly_once_across_polls() -> None:
    """A re-sent unanswered key is NOT re-asked: exactly one answer per key."""

    session = ScriptedSession(
        [
            _task_payload(
                "task-3",
                "input_required",
                poll_interval_ms=1,
                input_requests={"k1": _elicit_request("first?")},
            ),
            # The server has not processed the update yet and re-sends k1.
            _task_payload(
                "task-3",
                "input_required",
                poll_interval_ms=1,
                input_requests={"k1": _elicit_request("first?")},
            ),
            # Now a genuinely NEW key arrives alongside the still-listed k1.
            _task_payload(
                "task-3",
                "input_required",
                poll_interval_ms=1,
                input_requests={
                    "k1": _elicit_request("first?"),
                    "k2": _elicit_request("second?"),
                },
            ),
            _task_payload("task-3", "completed", result={"content": []}),
        ]
    )
    asked: list[str] = []

    async def recording_callback(context: Any, params: Any) -> Any:
        asked.append(params.message)
        return await _answer_everything(context, params)

    final = await drive_task_to_terminal(
        session, "task-3", recording_callback, store=_fresh_store()
    )

    assert final.status == "completed"
    # One prompt per KEY, never per poll — this is the whole point of the ledger.
    assert asked == ["first?", "second?"]
    assert [sorted(update) for update in session.updates] == [["k1"], ["k2"]]


async def test_input_requests_reach_the_one_hitl_surface_via_elicitation() -> None:
    """In-task input is dispatched through the client's elicitation callback.

    That callback is CLIO's #1113 correlated handler on every execution-path client,
    so a task's question lands on the SAME HITL surface as a foreground elicitation
    — no parallel prompt store.
    """

    session = ScriptedSession(
        [
            _task_payload(
                "task-4",
                "input_required",
                poll_interval_ms=1,
                input_requests={"k1": _elicit_request("region?")},
            ),
            _task_payload("task-4", "completed", result={"content": []}),
        ]
    )
    seen: list[Any] = []

    async def recording_callback(context: Any, params: Any) -> Any:
        seen.append((context.session, params.message))
        return await _answer_everything(context, params)

    await drive_task_to_terminal(session, "task-4", recording_callback, store=_fresh_store())

    assert len(seen) == 1
    dispatched_session, message = seen[0]
    assert dispatched_session is session
    assert message == "region?"


async def test_missing_elicitation_handler_is_a_typed_error() -> None:
    """A task asking for input on a client with no HITL surface fails loudly."""

    session = ScriptedSession(
        [
            _task_payload(
                "task-5",
                "input_required",
                poll_interval_ms=1,
                input_requests={"k1": _elicit_request("who?")},
            )
        ]
    )
    with pytest.raises(ToolError) as excinfo:
        await drive_task_to_terminal(session, "task-5", None, store=_fresh_store())
    assert excinfo.value.details["input_keys"] == ["k1"]


async def test_no_progress_input_required_rounds_are_bounded(monkeypatch: Any) -> None:
    """A server stuck re-sending only answered keys stops with a typed reason."""

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    parked = _task_payload(
        "task-6",
        "input_required",
        poll_interval_ms=1,
        input_requests={"k1": _elicit_request("who?")},
    )
    session = ScriptedSession([dict(parked) for _ in range(10)])
    ledger = TaskInputLedger()
    ledger.mark_answered("task-6", ["k1"])

    with pytest.raises(ToolError) as excinfo:
        await drive_task_to_terminal(
            session,
            "task-6",
            _answer_everything,
            ledger=ledger,
            store=_fresh_store(),
            max_no_progress_rounds=3,
        )
    assert excinfo.value.details["reason"] == MCP_TASK_INPUT_NO_PROGRESS
    assert excinfo.value.details["answered_keys"] == ["k1"]


# --------------------------------------------------------------------------- #
# Gap 2: Mcp-Name on task RPCs                                                #
# --------------------------------------------------------------------------- #


def test_task_requests_declare_name_param_so_the_sdk_stamps_mcp_name() -> None:
    """``Request.name_param`` is the SDK's own ``Mcp-Name`` hook — task RPCs set it.

    ``mcp.shared.inbound.NAME_BEARING_METHODS`` covers only tools/prompts/resources,
    so this per-request-class declaration is the ONLY thing that makes the session
    emit ``Mcp-Name`` for ``tasks/*``.
    """

    from mcp.shared.inbound import NAME_BEARING_METHODS

    from clio_agent.tools.mcp_tasks import (
        NamedCancelTaskRequest,
        NamedGetTaskRequest,
        NamedUpdateTaskRequest,
    )

    for request_cls in (NamedGetTaskRequest, NamedUpdateTaskRequest, NamedCancelTaskRequest):
        assert request_cls.name_param == "taskId"
    # The substrate's own models leave the hook unset — that is the gap CLIO closes.
    from fastmcp_tasks.client_models import GetTaskRequest

    assert GetTaskRequest.name_param is None
    assert "tasks/get" not in NAME_BEARING_METHODS


class _CapturingDispatcher:
    """A ``Dispatcher`` stub that records the HTTP header dict per raw request."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, str]]] = []

    async def send_raw_request(self, method: str, params: Any, opts: Any) -> dict[str, Any]:
        """Record the outbound headers and answer with a minimal Result."""

        self.sent.append((method, dict(opts.get("headers") or {})))
        return {}


async def test_mcp_name_header_is_emitted_for_task_rpcs() -> None:
    """Header capture on the REAL ``ClientSession`` send path (SEP-2663 MUST).

    Builds a genuine ``ClientSession`` with the modern per-request stamp installed
    and a capturing dispatcher, then sends each task RPC through CLIO's senders. The
    assertion is on the header dict the Streamable HTTP transport puts on the POST —
    not on a re-implementation of the stamping rule.
    """

    from mcp.client.session import ClientSession, _make_modern_stamp
    from mcp.shared.inbound import MCP_METHOD_HEADER, MCP_NAME_HEADER

    from clio_agent.tools.mcp_tasks import send_task_cancel, send_task_update

    dispatcher = _CapturingDispatcher()
    session = ClientSession(dispatcher=dispatcher)
    session._stamp = _make_modern_stamp(
        "2026-07-28",
        {"name": "clio-agent", "version": "test"},
        {},
        lambda name, args: {},
    )

    # tasks/get returns a typed result, so drive it through the poll loop's sender
    # only for the two RPCs whose result is a bare Result; tasks/get is asserted via
    # the same send path with a validating stub below.
    await send_task_update(session, "task-hdr", {})
    await send_task_cancel(session, "task-hdr")

    assert [method for method, _ in dispatcher.sent] == ["tasks/update", "tasks/cancel"]
    for method, headers in dispatcher.sent:
        assert headers[MCP_NAME_HEADER] == "task-hdr", f"{method} lost Mcp-Name"
        assert headers[MCP_METHOD_HEADER] == method


async def test_mcp_name_header_is_emitted_for_tasks_get() -> None:
    """``tasks/get`` — the RPC the poll loop sends most — carries ``Mcp-Name`` too."""

    from mcp.client.session import ClientSession, _make_modern_stamp
    from mcp.shared.inbound import MCP_NAME_HEADER

    from clio_agent.tools.mcp_tasks import send_task_get

    class _GetDispatcher(_CapturingDispatcher):
        """Answers ``tasks/get`` with a valid task payload."""

        async def send_raw_request(self, method: str, params: Any, opts: Any) -> Any:
            """Record headers, then return a completed task."""

            self.sent.append((method, dict(opts.get("headers") or {})))
            return _task_payload("task-hdr", "completed", result={"content": []})

    dispatcher = _GetDispatcher()
    session = ClientSession(dispatcher=dispatcher)
    session._stamp = _make_modern_stamp(
        "2026-07-28",
        {"name": "clio-agent", "version": "test"},
        {},
        lambda name, args: {},
    )

    result = await send_task_get(session, "task-hdr")

    assert result.status == "completed"
    assert dispatcher.sent[0][0] == "tasks/get"
    assert dispatcher.sent[0][1][MCP_NAME_HEADER] == "task-hdr"


# --------------------------------------------------------------------------- #
# Cancel: ack-only, split from #1116                                          #
# --------------------------------------------------------------------------- #


async def test_cancel_is_ack_only_and_emits_no_cancelled_notification() -> None:
    """``tasks/cancel`` is a REQUEST; no ``notifications/cancelled`` names a task."""

    store = _fresh_store()
    store.put(TaskRecord(task_id="task-8", status="working"))
    session = ScriptedSession([])

    await cancel_task(session, "task-8", store=store)

    assert session.methods() == ["tasks/cancel"]
    assert session.notifications == []
    assert store.get("task-8") is None


def test_removed_task_methods_are_never_called() -> None:
    """SEP-2663 Final has no ``tasks/list`` and no ``tasks/result``."""

    from pathlib import Path

    source = Path("src/clio_agent/tools/mcp_tasks.py").read_text(encoding="utf-8")
    for method in REMOVED_TASK_METHODS:
        # The constant itself is the only place the strings may appear.
        assert source.count(f'"{method}"') == 1


# --------------------------------------------------------------------------- #
# Declaration + the tasks-OFF guard for CLIO's own servers (#1119)            #
# --------------------------------------------------------------------------- #


def test_direct_execution_client_declares_the_tasks_extension() -> None:
    """A normal execution-path client folds CLIO's tasks extension in."""

    from fastmcp import Client

    declaration = tasks_declaration(None, Client)

    assert declaration.reason is None
    assert len(declaration.extensions) == 1
    assert isinstance(declaration.extensions[0], ClioTasksClientExtension)
    assert declaration.extensions[0].identifier == "io.modelcontextprotocol/tasks"


def test_proxy_backend_suppresses_the_declaration_with_a_typed_reason() -> None:
    """A proxy must not advertise task support to its backend (#1119)."""

    from fastmcp.server.providers.proxy import ProxyClient

    declaration = tasks_declaration(None, ProxyClient)

    assert declaration.extensions == ()
    assert declaration.reason == MCP_TASKS_DECLARATION_SUPPRESSED


def test_clio_builtin_servers_serve_no_tasks() -> None:
    """#1119: CLIO's own fs/shell servers never run a call as a task."""

    from fastmcp_tasks.extension import TasksExtension

    from clio_agent.tools.servers.fs_server import fs_server
    from clio_agent.tools.servers.shell_server import shell_server

    for server in (fs_server, shell_server):
        extensions = list(getattr(server, "extensions", ()) or ())
        assert not any(isinstance(ext, TasksExtension) for ext in extensions)


def test_make_mcp_client_declares_tasks_on_execution_clients() -> None:
    """``make_mcp_client`` is the site that turns the declaration on."""

    from clio_agent.tools.mcp_runtime import make_mcp_client

    captured: dict[str, Any] = {}

    class FakeClient:
        """Construction-inspecting client double."""

        _auto_internal_extensions = True

        def __init__(self, target: Any, **kwargs: Any) -> None:
            captured["target"] = target
            captured.update(kwargs)

    make_mcp_client(object(), client_cls=FakeClient)
    extensions = captured.get("extensions") or []

    assert len(extensions) == 1
    assert isinstance(extensions[0], ClioTasksClientExtension)


def test_make_mcp_client_can_opt_out_of_tasks() -> None:
    """``tasks=False`` builds a client that declares nothing."""

    from clio_agent.tools.mcp_runtime import make_mcp_client

    captured: dict[str, Any] = {}

    class FakeClient:
        """Construction-inspecting client double."""

        _auto_internal_extensions = True

        def __init__(self, target: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

    make_mcp_client(object(), client_cls=FakeClient, tasks=False)

    assert "extensions" not in captured


# --------------------------------------------------------------------------- #
# #1112 classification: a handled task result is not a degrade                #
# --------------------------------------------------------------------------- #


def test_task_result_type_is_handled_not_downgraded() -> None:
    """``resultType: "task"`` is a shape this client drives, so it never degrades."""

    from clio_agent.tools.mcp_results import (
        call_tool_result_to_observer,
        classify_call_tool_result,
    )

    classification = classify_call_tool_result({"resultType": "task"})

    assert classification.result_type == "task"
    assert classification.degrade_reason is None
    assert classification.explicitly_carried is True

    observed = call_tool_result_to_observer({"content": [], "resultType": "task"})
    assert observed["resultType"] == "task"
    assert "degrade" not in observed


def test_unknown_result_type_still_degrades() -> None:
    """A result type this client cannot act on is still a typed downgrade."""

    from clio_agent.tools.mcp_results import classify_call_tool_result

    classification = classify_call_tool_result({"resultType": "streaming"})

    assert classification.result_type == "complete"
    assert classification.degrade_reason == "mcp_result_downgraded_to_complete"
    assert classification.original_result_type == "streaming"


# --------------------------------------------------------------------------- #
# The process store registry                                                  #
# --------------------------------------------------------------------------- #


def test_absent_durable_store_degrades_to_memory_with_a_typed_reason(
    caplog: Any,
) -> None:
    """No durable home is a REPORTED degradation, never a silent one."""

    import logging

    set_task_record_store(None)
    with caplog.at_level(logging.WARNING, logger="clio_agent.tools.mcp_task_records"):
        store = task_record_store()
    try:
        assert isinstance(store, InMemoryTaskRecordStore)
        assert "mcp_task_record_store_absent" in caplog.text
    finally:
        set_task_record_store(None)
