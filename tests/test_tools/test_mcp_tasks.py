"""Tasks extension client (SEP-2663, #1115).

The substrate is the **`fastmcp_tasks`** package (note: `mcp/client/experimental/tasks.py`
does NOT exist in the installed mcp 2.0 SDK). It provides the client extension, the
`tasks/get` poll loop honoring `pollIntervalMs`, `input_required` rounds via
`tasks/update` dispatched through the client's elicitation callback (CLIO's P1.3
handler, so inputs land on the ONE HITL surface), and ack-only `tasks/cancel`.

CLIO builds what the substrate lacks: retry- and concurrency-safe input dedup (the
answer PAYLOAD persisted before transmission, plus an exclusive per-task lease), the
`Mcp-Name: <taskId>` header on task RPCs, composite-keyed durable persistence +
reconnect-by-identity, and #1112 classification tolerance for `resultType: "task"`.

These tests drive the poll loop against a scripted fake `ClientSession` that records
every request it is sent, so the wire assertions (which method, which params, which
header the SDK would stamp) are made on the objects the SDK itself consumes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from clio_agent.errors import (
    MCP_TASK_INPUT_NO_PROGRESS,
    MCP_TASK_LEASE_HELD,
    MCP_TASK_RECORD_NOT_DURABLE,
    MCP_TASKS_DECLARATION_SUPPRESSED,
    ToolError,
)
from clio_agent.tools.mcp_task_extension import (
    BackendIdentity,
    ClioTasksClientExtension,
    backend_identity,
    tasks_declaration,
)
from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskInputAnswer,
    TaskInputLedger,
    TaskKey,
    TaskLease,
    TaskRecord,
    open_task_records,
    set_task_record_store,
)
from clio_agent.tools.mcp_tasks import (
    REMOVED_TASK_METHODS,
    cancel_task,
    drive_task_to_terminal,
    resume_task,
    task_record_store,
)

SERVER_A = "server-a"
SERVER_B = "server-b"


def _key(task_id: str, *, server: str = SERVER_A, session: str | None = "sess-1") -> TaskKey:
    """A composite task identity for the tests."""

    return TaskKey(server_id=server, session_id=session, task_id=task_id)


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

    ``update_failures`` scripts transport-level failures for `tasks/update`: the
    number of leading updates that raise instead of returning, which is how a lost
    acknowledgement and a rejected update are simulated.
    """

    def __init__(
        self,
        script: list[dict[str, Any]],
        *,
        update_failures: int = 0,
        update_error: Exception | None = None,
    ) -> None:
        self._script = list(script)
        self._update_failures = update_failures
        self._update_error = update_error or RuntimeError("tasks/update acknowledgement lost")
        self.requests: list[Any] = []
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
            return result_type.model_validate(self._script.pop(0))
        if method == "tasks/update":
            self.updates.append(dict(request.params.input_responses))
            if self._update_failures > 0:
                self._update_failures -= 1
                raise self._update_error
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


class _Ctx:
    """Minimal ``ClaimContext`` stand-in."""

    def __init__(self, session: Any, read_timeout_seconds: float | None = None) -> None:
        self.session = session
        self.read_timeout_seconds = read_timeout_seconds


def _fresh_store() -> InMemoryTaskRecordStore:
    """A store isolated from the process registry."""

    return InMemoryTaskRecordStore()


def _create_result(task_id: str) -> Any:
    """A claimed ``CreateTaskResult`` as a task-serving backend returns it."""

    from fastmcp_tasks.client_models import ClientCreateTaskResult

    return ClientCreateTaskResult.model_validate(
        {
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-07-31T00:00:00+00:00",
            "lastUpdatedAt": "2026-07-31T00:00:00+00:00",
            "resultType": "task",
        }
    )


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
    key = _key("task-42")
    # The client that started the task persisted the id, then died mid-flight.
    store.put(TaskRecord(key=key, tool="slow_tool", status="working"))

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

    final = await resume_task(session, key, store=store)

    assert final.status == "completed"
    assert session.methods() == ["tasks/get", "tasks/get"]
    # #1205 review D1 (2nd round): settled tasks are RETAINED with their
    # terminal status (matches AgentTask's dismissed-field semantics; removal
    # is an explicit later dismiss, never automatic at settle) — a later
    # reconnect sweep still skips them via open_task_records()'s own
    # `status not in TERMINAL_TASK_STATES` filter, not via deletion.
    settled = store.get(key)
    assert settled is not None
    assert settled.status == "completed"
    assert key not in {r.key for r in open_task_records(store)}


async def test_resume_without_a_persisted_record_is_a_typed_error() -> None:
    """Resuming an unknown identity raises rather than inventing a poll loop."""

    session = ScriptedSession([])
    with pytest.raises(ToolError) as excinfo:
        await resume_task(session, _key("task-unknown"), store=_fresh_store())
    assert excinfo.value.details["task_id"] == "task-unknown"
    assert session.requests == []


async def test_resume_seeds_the_dedup_ledger_from_the_persisted_record() -> None:
    """A key answered AND delivered before the crash is not asked again after it."""

    store = _fresh_store()
    key = _key("task-7")
    store.put(
        TaskRecord(
            key=key,
            status="input_required",
            input_answers=(
                TaskInputAnswer(key="k1", payload={"action": "accept"}, delivered=True),
            ),
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

    final = await resume_task(session, key, elicitation_callback=recording_callback, store=store)

    assert final.status == "completed"
    assert prompts == []
    # The stored payload is RETRANSMITTED verbatim rather than suppressed, so a
    # server that never saw the pre-crash update is not left starving.
    assert session.updates == [{"k1": {"action": "accept"}}]


# --------------------------------------------------------------------------- #
# Poll loop + pollIntervalMs                                                  #
# --------------------------------------------------------------------------- #


async def test_poll_loop_honors_server_poll_interval_ms() -> None:
    """The server-advertised ``pollIntervalMs`` caps the client's poll cadence."""

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    session = ScriptedSession(
        [_task_payload("task-1", "working", poll_interval_ms=250) for _ in range(5)]
        + [_task_payload("task-1", "completed", result={"content": []})]
    )

    final = await drive_task_to_terminal(
        session,
        _key("task-1"),
        store=_fresh_store(),
        poll_sleep=fake_sleep,
    )

    assert final.status == "completed"
    # The ramp starts fast so a quick task resolves promptly, then settles AT the
    # server's advertised cadence and never exceeds it.
    assert slept, "a working task must sleep between polls"
    assert max(slept) <= 0.25
    assert slept[-1] == pytest.approx(0.25)
    assert slept == sorted(slept), "the backoff must ramp monotonically to the ceiling"


async def test_create_task_result_is_driven_to_the_real_result() -> None:
    """The extension resolves a claimed ``CreateTaskResult`` into the tool's result."""

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
        extension = ClioTasksClientExtension(BackendIdentity(SERVER_A, {"transport": "test"}))
        result = await extension._resolve_task(_create_result("task-9"), _Ctx(session))

        assert result.content[0].text == "42"
        assert result.is_error in (False, None)
        # #1205 review D1 (2nd round): the id was persisted under the extension's
        # backend identity, then RETAINED with its terminal status — not dropped
        # (matches AgentTask's dismissed-field semantics; removal is an explicit
        # later dismiss, never automatic at settle).
        records = store.list()
        assert len(records) == 1
        assert records[0].task_id == "task-9"
        assert records[0].status == "completed"
    finally:
        set_task_record_store(None)


# --------------------------------------------------------------------------- #
# Finding 1: dedup is retry-safe and concurrency-safe                         #
# --------------------------------------------------------------------------- #


async def test_input_key_answered_exactly_once_across_polls() -> None:
    """A re-sent unanswered key is NOT re-asked: exactly one prompt per key."""

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
        session, _key("task-3"), recording_callback, store=_fresh_store()
    )

    assert final.status == "completed"
    # One prompt per KEY, never per poll — the whole point of the ledger.
    assert asked == ["first?", "second?"]
    # Every round retransmits the outstanding keys' stored payloads verbatim.
    assert [sorted(update) for update in session.updates] == [["k1"], ["k1"], ["k1", "k2"]]


async def test_lost_update_acknowledgement_retries_the_identical_payload() -> None:
    """A ``tasks/update`` whose response is lost is retried, never re-elicited.

    The human's answer is persisted BEFORE transmission, so the failed round leaves a
    recoverable payload and the next drive re-sends exactly those bytes.
    """

    store = _fresh_store()
    key = _key("task-lost")
    store.put(TaskRecord(key=key, status="working"))
    parked = _task_payload(
        "task-lost",
        "input_required",
        poll_interval_ms=1,
        input_requests={"k1": _elicit_request("who?")},
    )
    session = ScriptedSession(
        [dict(parked), dict(parked), _task_payload("task-lost", "completed", result={})],
        update_failures=1,
    )
    asked: list[str] = []

    async def recording_callback(context: Any, params: Any) -> Any:
        asked.append(params.message)
        return await _answer_everything(context, params)

    with pytest.raises(RuntimeError):
        await drive_task_to_terminal(session, key, recording_callback, store=store)

    # The answer survived the failed transmission, marked NOT delivered.
    persisted = store.get(key)
    assert persisted is not None
    assert [(a.key, a.delivered) for a in persisted.input_answers] == [("k1", False)]
    stored_payload = persisted.input_answers[0].payload

    # Resuming re-sends the IDENTICAL payload without asking the human again.
    final = await drive_task_to_terminal(session, key, recording_callback, store=store)

    assert final.status == "completed"
    assert asked == ["who?"], "the human must be asked exactly once across both drives"
    assert session.updates == [{"k1": stored_payload}, {"k1": stored_payload}]


async def test_rejected_update_retries_the_identical_payload() -> None:
    """A server that REJECTS the update gets the same bytes back, not a new prompt."""

    from mcp.shared.exceptions import MCPError

    store = _fresh_store()
    key = _key("task-rejected")
    store.put(TaskRecord(key=key, status="working"))
    parked = _task_payload(
        "task-rejected",
        "input_required",
        poll_interval_ms=1,
        input_requests={"k1": _elicit_request("who?")},
    )
    session = ScriptedSession(
        [dict(parked), dict(parked), _task_payload("task-rejected", "completed", result={})],
        update_failures=1,
        update_error=MCPError(code=-32602, message="stale update"),
    )
    asked: list[str] = []

    async def recording_callback(context: Any, params: Any) -> Any:
        asked.append(params.message)
        return await _answer_everything(context, params)

    with pytest.raises(MCPError):
        await drive_task_to_terminal(session, key, recording_callback, store=store)
    final = await drive_task_to_terminal(session, key, recording_callback, store=store)

    assert final.status == "completed"
    assert asked == ["who?"]
    assert session.updates[0] == session.updates[1]


async def test_server_ledger_divergence_retransmits_instead_of_starving(
    monkeypatch: Any,
) -> None:
    """A server re-reporting a DELIVERED key is retried, then bounded — never starved.

    A key-only ledger suppressed retransmission on divergence, so the task could only
    end at the no-progress abort. Now the stored payload goes out on every round and
    the bound is the backstop rather than the outcome.
    """

    async def fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    store = _fresh_store()
    key = _key("task-diverged")
    store.put(
        TaskRecord(
            key=key,
            status="input_required",
            input_answers=(
                TaskInputAnswer(key="k1", payload={"action": "accept"}, delivered=True),
            ),
        )
    )
    parked = _task_payload(
        "task-diverged",
        "input_required",
        poll_interval_ms=1,
        input_requests={"k1": _elicit_request("who?")},
    )
    session = ScriptedSession([dict(parked) for _ in range(10)])

    with pytest.raises(ToolError) as excinfo:
        await drive_task_to_terminal(
            session, key, _answer_everything, store=store, max_no_progress_rounds=3
        )

    assert excinfo.value.details["reason"] == MCP_TASK_INPUT_NO_PROGRESS
    assert excinfo.value.details["delivered_keys"] == ["k1"]
    # Retransmitted every round, identically — not suppressed until the abort.
    assert len(session.updates) == 4
    assert all(update == {"k1": {"action": "accept"}} for update in session.updates)


async def test_two_concurrent_resumes_cannot_both_drive_one_task() -> None:
    """The lease refuses the second driver instead of double-prompting."""

    store = _fresh_store()
    key = _key("task-leased")
    store.put(TaskRecord(key=key, status="working"))

    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingSession(ScriptedSession):
        """Parks inside the first ``tasks/get`` so both drivers overlap."""

        async def send_request(self, request: Any, result_type: Any, **kwargs: Any) -> Any:
            """Signal that the drive is live, then wait before answering."""

            started.set()
            await release.wait()
            return await super().send_request(request, result_type, **kwargs)

    first = BlockingSession([_task_payload("task-leased", "completed", result={})])
    driver = asyncio.create_task(drive_task_to_terminal(first, key, store=store))
    await asyncio.wait_for(started.wait(), timeout=5)

    second = ScriptedSession([_task_payload("task-leased", "completed", result={})])
    with pytest.raises(ToolError) as excinfo:
        await resume_task(second, key, store=store)

    release.set()
    final = await asyncio.wait_for(driver, timeout=5)

    assert final.status == "completed"
    assert excinfo.value.details["reason"] == MCP_TASK_LEASE_HELD
    assert second.requests == [], "the refused driver must not touch the wire"


def test_expired_lease_is_reclaimable() -> None:
    """A lease from a process that died mid-drive must not wedge the task forever."""

    import time

    store = _fresh_store()
    key = _key("task-stale-lease")
    store.put(
        TaskRecord(
            key=key,
            status="working",
            lease_owner="dead-process:1",
            lease_expires_at=time.time() - 1.0,
        )
    )

    lease = TaskLease(store, key, owner="live-driver")
    lease.acquire()
    try:
        held = store.get(key)
        assert held is not None
        assert held.lease_owner == "live-driver"
    finally:
        lease.release()
    assert (store.get(key) or TaskRecord(key=key)).lease_owner is None


def test_ledger_state_machine_is_absent_captured_delivered() -> None:
    """The payload is retained through every state, so a retry is always identical."""

    ledger = TaskInputLedger()
    assert ledger.unelicited(["k1"]) == ["k1"]

    ledger.capture("k1", {"action": "accept", "content": {"answer": "yes"}})
    assert ledger.unelicited(["k1"]) == []
    assert ledger.delivered_keys() == frozenset()
    assert ledger.payloads_for(["k1"]) == {"k1": {"action": "accept", "content": {"answer": "yes"}}}

    ledger.mark_delivered(["k1"])
    assert ledger.delivered_keys() == frozenset({"k1"})
    # A delivered key still yields its payload, so a divergence retransmits.
    assert ledger.payloads_for(["k1"])["k1"]["action"] == "accept"


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

    await drive_task_to_terminal(session, _key("task-4"), recording_callback, store=_fresh_store())

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
        await drive_task_to_terminal(session, _key("task-5"), None, store=_fresh_store())
    assert excinfo.value.details["input_keys"] == ["k1"]


# --------------------------------------------------------------------------- #
# Finding 2: composite identity                                               #
# --------------------------------------------------------------------------- #


def test_same_task_id_from_two_servers_does_not_overwrite() -> None:
    """A server-minted id is unique only WITHIN its server."""

    store = _fresh_store()
    a = _key("shared-id", server=SERVER_A)
    b = _key("shared-id", server=SERVER_B)
    store.put(TaskRecord(key=a, tool="tool-a"))
    store.put(TaskRecord(key=b, tool="tool-b"))

    assert (store.get(a) or TaskRecord(key=a)).tool == "tool-a"
    assert (store.get(b) or TaskRecord(key=b)).tool == "tool-b"
    assert len(store.list()) == 2


def test_dropping_one_server_task_leaves_the_other_alive() -> None:
    """A ``drop`` must never delete an unrelated backend's live crash-recovery record."""

    store = _fresh_store()
    a = _key("shared-id", server=SERVER_A)
    b = _key("shared-id", server=SERVER_B)
    store.put(TaskRecord(key=a))
    store.put(TaskRecord(key=b))

    store.drop(a)

    assert store.get(a) is None
    assert store.get(b) is not None


def test_same_task_id_in_two_sessions_does_not_collide() -> None:
    """Two CLIO sessions can hold the same server's task id independently."""

    store = _fresh_store()
    one = _key("shared-id", session="sess-1")
    two = _key("shared-id", session="sess-2")
    store.put(TaskRecord(key=one, tool="one"))
    store.put(TaskRecord(key=two, tool="two"))

    store.drop(one)

    assert store.get(one) is None
    assert (store.get(two) or TaskRecord(key=two)).tool == "two"


def test_backend_identity_is_stable_and_distinguishing() -> None:
    """The same backend digests identically; different backends never collide."""

    class Http:
        """An http-shaped transport double."""

        def __init__(self, url: str) -> None:
            self.url = url

    class Stdio:
        """A stdio-shaped transport double."""

        command = "python"
        args = ["-m", "server"]

    first = backend_identity(Http("http://127.0.0.1:9/mcp"))
    again = backend_identity(Http("http://127.0.0.1:9/mcp"))
    other = backend_identity(Http("http://127.0.0.1:10/mcp"))
    stdio = backend_identity(Stdio())

    assert first.server_id == again.server_id
    assert first.server_id != other.server_id
    assert first.locator == {"transport": "http", "url": "http://127.0.0.1:9/mcp"}
    assert stdio.locator == {"transport": "stdio", "command": "python", "args": ["-m", "server"]}
    assert len({first.server_id, other.server_id, stdio.server_id}) == 3


async def test_resume_requires_the_full_identity() -> None:
    """A record under one server is not resumable under another's identity."""

    store = _fresh_store()
    store.put(TaskRecord(key=_key("task-x", server=SERVER_A), status="working"))
    session = ScriptedSession([])

    with pytest.raises(ToolError):
        await resume_task(session, _key("task-x", server=SERVER_B), store=store)
    assert session.requests == []


# --------------------------------------------------------------------------- #
# Finding 4: create-before-durable window                                     #
# --------------------------------------------------------------------------- #


async def test_persistence_failure_is_a_typed_recovery_error_with_a_cancel_attempt() -> None:
    """A task that cannot be made durable fails LOUDLY, carrying everything to reconcile."""

    class FailingStore(InMemoryTaskRecordStore):
        """A store whose durable write fails (the failpoint DURING store.put)."""

        def put(self, record: TaskRecord) -> None:
            """Fail every durable write."""

            raise OSError("disk full")

    session = ScriptedSession([])
    identity = BackendIdentity(SERVER_A, {"transport": "http", "url": "http://x/mcp"})
    set_task_record_store(FailingStore())
    try:
        extension = ClioTasksClientExtension(identity)
        with pytest.raises(ToolError) as excinfo:
            await extension._resolve_task(_create_result("task-orphan"), _Ctx(session))
    finally:
        set_task_record_store(None)

    details = excinfo.value.details
    assert details["reason"] == MCP_TASK_RECORD_NOT_DURABLE
    assert details["task_id"] == "task-orphan"
    assert details["server_id"] == SERVER_A
    assert details["backend"] == {"transport": "http", "url": "http://x/mcp"}
    # A best-effort tasks/cancel is the only lever a client has over an orphan.
    assert details["cancel_attempted"] is True
    assert session.methods() == ["tasks/cancel"]
    # It never proceeds to poll a task it cannot recover.
    assert "tasks/get" not in session.methods()


async def test_no_polling_happens_before_the_record_is_durable() -> None:
    """The failpoint immediately BEFORE store.put: nothing is driven yet."""

    order: list[str] = []

    class RecordingStore(InMemoryTaskRecordStore):
        """Records the ordering of the durable write against the wire."""

        def put(self, record: TaskRecord) -> None:
            """Note the write, then persist."""

            order.append(f"put:{record.key.task_id}")
            super().put(record)

    class OrderedSession(ScriptedSession):
        """Notes every RPC so the write/poll ordering is assertable."""

        async def send_request(self, request: Any, result_type: Any, **kwargs: Any) -> Any:
            """Note the RPC, then answer it."""

            order.append(request.method)
            return await super().send_request(request, result_type, **kwargs)

    session = OrderedSession([_task_payload("task-order", "completed", result={"content": []})])
    set_task_record_store(RecordingStore())
    try:
        extension = ClioTasksClientExtension(BackendIdentity(SERVER_A, {}))
        await extension._resolve_task(_create_result("task-order"), _Ctx(session))
    finally:
        set_task_record_store(None)

    assert order[0] == "put:task-order", f"durable write must precede any RPC: {order}"
    assert "tasks/get" in order


# --------------------------------------------------------------------------- #
# Gap 2: Mcp-Name on task RPCs                                                #
# --------------------------------------------------------------------------- #


def test_task_requests_declare_name_param_so_the_sdk_stamps_mcp_name() -> None:
    """``Request.name_param`` is the SDK's own ``Mcp-Name`` hook — task RPCs set it.

    ``mcp.shared.inbound.NAME_BEARING_METHODS`` covers only tools/prompts/resources,
    so this per-request-class declaration is the ONLY thing that makes the session
    emit ``Mcp-Name`` for ``tasks/*``.
    """

    from fastmcp_tasks.client_models import GetTaskRequest
    from mcp.shared.inbound import NAME_BEARING_METHODS

    from clio_agent.tools.mcp_tasks import (
        NamedCancelTaskRequest,
        NamedGetTaskRequest,
        NamedUpdateTaskRequest,
    )

    for request_cls in (NamedGetTaskRequest, NamedUpdateTaskRequest, NamedCancelTaskRequest):
        assert request_cls.name_param == "taskId"
    # The substrate's own models leave the hook unset — that is the gap CLIO closes.
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
        "2026-07-28", {"name": "clio-agent", "version": "test"}, {}, lambda name, args: {}
    )

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
        "2026-07-28", {"name": "clio-agent", "version": "test"}, {}, lambda name, args: {}
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
    key = _key("task-8")
    store.put(TaskRecord(key=key, status="working"))
    session = ScriptedSession([])

    await cancel_task(session, key, store=store)

    assert session.methods() == ["tasks/cancel"]
    assert session.notifications == []
    # #1205 review D1 (2nd round): RETAINED with its final status, not dropped —
    # matches AgentTask's dismissed-field semantics; removal is an explicit
    # later dismiss (run_registry.dismiss_run), never automatic at settle.
    cancelled = store.get(key)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


async def test_cancel_stamps_only_the_named_identity() -> None:
    """Cancelling one backend's task marks only IT cancelled, leaving another
    backend's same-id task's status untouched (composite-key scoping, #1205
    review 2nd round — was "...drops only..." before cancel stopped dropping)."""

    store = _fresh_store()
    a = _key("shared-id", server=SERVER_A)
    b = _key("shared-id", server=SERVER_B)
    store.put(TaskRecord(key=a, status="working"))
    store.put(TaskRecord(key=b, status="working"))

    await cancel_task(ScriptedSession([]), a, store=store)

    stamped = store.get(a)
    assert stamped is not None
    assert stamped.status == "cancelled"
    untouched = store.get(b)
    assert untouched is not None
    assert untouched.status == "working"


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

    declaration = tasks_declaration(Client, object())

    assert declaration.reason is None
    assert len(declaration.extensions) == 1
    assert isinstance(declaration.extensions[0], ClioTasksClientExtension)
    assert declaration.extensions[0].identifier == "io.modelcontextprotocol/tasks"
    assert declaration.extensions[0].backend.server_id


def test_proxy_backend_suppresses_the_declaration_with_a_typed_reason() -> None:
    """A proxy must not advertise task support to its backend (#1119)."""

    from fastmcp.server.providers.proxy import ProxyClient

    declaration = tasks_declaration(ProxyClient, object())

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

    class Transport:
        """An http-shaped transport double, so the identity is derived from it."""

        url = "http://127.0.0.1:1234/mcp"

    transport = Transport()
    make_mcp_client(transport, client_cls=FakeClient)
    extensions = captured.get("extensions") or []

    assert len(extensions) == 1
    assert isinstance(extensions[0], ClioTasksClientExtension)
    assert extensions[0].backend.server_id == backend_identity(transport).server_id


def test_make_mcp_client_omits_the_declaration_for_a_proxy_client_class() -> None:
    """The one honest suppression is the client class's own ``_auto_internal_extensions``.

    There is deliberately no per-call opt-out: importing ``fastmcp_tasks`` registers
    an internal factory process-wide, so a client that declared nothing would still
    fold the substrate's own extension — an "off" switch would swap CLIO's hardened
    resolver for the un-hardened one instead of turning the advertisement off.
    """

    from clio_agent.tools.mcp_runtime import make_mcp_client

    captured: dict[str, Any] = {}

    class ProxyLikeClient:
        """A client class that forbids internal extensions, as ``ProxyClient`` does."""

        _auto_internal_extensions = False

        def __init__(self, target: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

    make_mcp_client(object(), client_cls=ProxyLikeClient)

    assert "extensions" not in captured


def test_no_tool_dispatching_client_is_built_outside_the_factory() -> None:
    """The bare-``Client()`` sites in src/ are list-only, so none can start a task.

    Importing this module registers ``fastmcp-tasks``' internal extension factory
    process-wide, so a bare ``fastmcp.Client`` would fold the SUBSTRATE's resolver
    (no dedup, no ``Mcp-Name``, no durable id). That is harmless only while every
    bare client is list-only. This guard pins that: any new bare ``Client(...)`` in
    ``src/`` must either be list-only or move to ``make_mcp_client``.
    """

    import re
    from pathlib import Path

    allowed = {
        # Documented list-only introspection sites (#1106/#1111).
        "src/clio_agent/tools/gateway.py",
        "src/clio_agent/gact/routes/catalog.py",
        "src/clio_agent/gact/routes/blueprints.py",
        "src/clio_agent/gact/routes/mcp.py",
        "src/clio_agent/runtime/status.py",
    }
    pattern = re.compile(r"(?<![.\w])Client\(")
    offenders = []
    for path in Path("src/clio_agent").rglob("*.py"):
        rel = path.as_posix()
        if rel in allowed or rel.endswith("mcp_runtime.py"):
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(rel)

    assert offenders == [], (
        "these modules construct a bare fastmcp Client outside make_mcp_client; if "
        "they dispatch a tool call they would silently use the un-hardened tasks "
        f"resolver: {offenders}"
    )


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


def test_absent_durable_store_degrades_to_memory_with_a_typed_reason(caplog: Any) -> None:
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


# --------------------------------------------------------------------------- #
# #1236 (clio-relay#265's client half, owner ruling 2026-08-20): effective    #
# status derivation. SEP-2663 "completed" only means DELIVERED, not that the  #
# application call itself succeeded -- a delivered result carrying            #
# isError=true must derive to "failed", never surface as bare "completed".    #
# --------------------------------------------------------------------------- #


def _get_result(
    status: str,
    *,
    result: dict[str, Any] | None = None,
    task_id: str = "task-eff",
) -> Any:
    """A ``ClientGetTaskResult`` as ``tasks/get`` returns it -- pure model
    construction, no network/session involved (mirrors ``_task_payload`` but
    returns the validated object ``derive_effective_status`` consumes)."""

    from fastmcp_tasks.client_models import ClientGetTaskResult

    return ClientGetTaskResult.model_validate(_task_payload(task_id, status, result=result))


@pytest.mark.parametrize("status", ["working", "input_required", "failed", "cancelled"])
def test_derive_effective_status_passes_every_non_completed_status_through_unchanged(
    status: str,
) -> None:
    """Only a ``"completed"`` delivery is ever inspected -- every other protocol
    status (including a genuine ``failed``) passes through byte-identical, with
    no reason attached. This is protocol-truth derivation, never a heuristic
    that second-guesses a status the server already reported honestly."""

    from clio_agent.tools.mcp_tasks import derive_effective_status

    effective, reason = derive_effective_status(_get_result(status))
    assert effective == status
    assert reason is None


def test_derive_effective_status_a_clean_completion_stays_completed() -> None:
    from clio_agent.tools.mcp_tasks import derive_effective_status

    result = {"content": [{"type": "text", "text": "all good"}]}
    effective, reason = derive_effective_status(_get_result("completed", result=result))
    assert effective == "completed"
    assert reason is None


def test_derive_effective_status_downgrades_a_delivered_error_result() -> None:
    """The exhibit from clio-relay#265's enforcement-stack reprobe: a relay job
    that genuinely failed (MPI rejected it, exit 186) still delivers as SEP-2663
    ``status: "completed"`` because delivering an error IS a completed dispatch
    -- the failure travels in the result's own ``isError``, not the status
    field. This is exactly what must derive to ``"failed"``."""

    from clio_agent.tools.mcp_tasks import derive_effective_status

    result = {
        "isError": True,
        "content": [{"type": "text", "text": "LAMMPS exited with code 186"}],
    }
    effective, reason = derive_effective_status(_get_result("completed", result=result))
    assert effective == "failed"
    assert reason == "LAMMPS exited with code 186"


def test_derive_effective_status_joins_multiple_text_blocks() -> None:
    from clio_agent.tools.mcp_tasks import derive_effective_status

    result = {
        "isError": True,
        "content": [
            {"type": "text", "text": "first line"},
            {"type": "image", "data": "irrelevant"},
            {"type": "text", "text": "second line"},
        ],
    }
    _, reason = derive_effective_status(_get_result("completed", result=result))
    assert reason == "first line\nsecond line"


def test_derive_effective_status_iserror_with_no_text_content_has_a_named_placeholder() -> None:
    """An error result with no text block (e.g. a binary-only content list)
    still derives to "failed" -- the ABSENCE of extractable text is never
    treated as the absence of an error."""

    from clio_agent.tools.mcp_tasks import derive_effective_status

    result = {"isError": True, "content": []}
    effective, reason = derive_effective_status(_get_result("completed", result=result))
    assert effective == "failed"
    assert reason is not None and "isError" in reason


def test_derive_effective_status_reason_is_capped() -> None:
    from clio_agent.tools.mcp_tasks import (
        EFFECTIVE_STATUS_REASON_MAX_CHARS,
        derive_effective_status,
    )

    huge = "x" * (EFFECTIVE_STATUS_REASON_MAX_CHARS * 3)
    result = {"isError": True, "content": [{"type": "text", "text": huge}]}
    _, reason = derive_effective_status(_get_result("completed", result=result))
    assert reason is not None
    assert len(reason) == EFFECTIVE_STATUS_REASON_MAX_CHARS


async def test_the_real_poll_loop_stamps_effective_status_on_a_delivered_error() -> None:
    """End-to-end through ``resume_task`` -> ``_record_status`` (not the pure
    helper in isolation): a completed-with-isError delivery must land on the
    STORED record with the raw ``status`` preserved (never destroyed) and
    ``effective_status``/``effective_status_reason`` carrying the honest
    derivation, so ``display_status`` (what a run card/SSE event actually
    reads, #1236) says "failed"."""

    store = _fresh_store()
    key = _key("task-real-error")
    store.put(TaskRecord(key=key, tool="jarvis_run", status="working"))
    session = ScriptedSession(
        [
            _task_payload(
                "task-real-error",
                "completed",
                result={
                    "isError": True,
                    "content": [{"type": "text", "text": "exit code 186"}],
                },
            ),
        ]
    )

    final = await resume_task(session, key, store=store)

    assert final.status == "completed", "the RAW protocol status is unchanged"
    settled = store.get(key)
    assert settled is not None
    assert settled.status == "completed", "raw status is never destroyed"
    assert settled.effective_status == "failed"
    assert settled.effective_status_reason == "exit code 186"
    assert settled.display_status == "failed"


async def test_the_real_poll_loop_leaves_a_clean_completion_effective_status_completed() -> None:
    store = _fresh_store()
    key = _key("task-real-clean")
    store.put(TaskRecord(key=key, tool="jarvis_run", status="working"))
    session = ScriptedSession(
        [_task_payload("task-real-clean", "completed", result={"content": []})]
    )

    await resume_task(session, key, store=store)

    settled = store.get(key)
    assert settled is not None
    assert settled.effective_status == "completed"
    assert settled.effective_status_reason is None
    assert settled.display_status == "completed"


async def test_cancel_task_stamps_effective_status_cancelled_alongside_status() -> None:
    """``cancel_task`` is ack-only (no later ``tasks/get`` to derive from) --
    it stamps ``effective_status="cancelled"`` directly rather than leaving a
    stale pre-cancel effective_status behind for ``display_status`` to read."""

    class _AckSession:
        async def send_request(
            self, request: Any, result_type: Any, request_read_timeout_seconds: float | None = None
        ) -> Any:
            return result_type()

    store = _fresh_store()
    key = _key("task-real-cancel")
    store.put(TaskRecord(key=key, tool="jarvis_run", status="working", effective_status="working"))

    from clio_agent.tools.mcp_tasks import cancel_task

    await cancel_task(_AckSession(), key, store=store)

    settled = store.get(key)
    assert settled is not None
    assert settled.status == "cancelled"
    assert settled.effective_status == "cancelled"
    assert settled.effective_status_reason is None
    assert settled.display_status == "cancelled"
