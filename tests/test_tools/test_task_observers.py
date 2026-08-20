"""#1231: the per-backend ``on_poll`` observer registry (tools/task_observers.py).

Two test groups:

1. The registry itself, in isolation — register/resolve/unregister, an unknown
   ``server_id`` resolving to ``None``, and a raising factory degrading to ``None``
   with a typed warning instead of propagating.
2. The transparent SEP-2663 client-extension wiring
   (``ClioTasksClientExtension._resolve_task`` -> ``drive_task_to_terminal``)
   actually calling the resolved hook for a REGISTERED backend, and NOT for an
   unregistered one — the run-14 gap this issue closes (13 relay-driven task
   records, all console bytes 0, because nothing was ever wired into this path).
   The scripted-session fake mirrors ``test_mcp_tasks.py``'s, kept self-contained
   here rather than imported so a failure in this file points straight at the
   registry wiring, never at a shared fixture two files depend on.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from clio_agent.errors import MCP_TASK_OBSERVER_FACTORY_FAILED
from clio_agent.tools.mcp_task_extension import BackendIdentity, ClioTasksClientExtension
from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskKey,
    set_task_record_store,
)
from clio_agent.tools.task_observers import (
    register_task_observer_factory,
    resolve_task_observer,
    unregister_task_observer_factory,
)

SERVER_A = "observer-server-a"
SERVER_B = "observer-server-b"


def _key(task_id: str, *, server: str = SERVER_A) -> TaskKey:
    """A composite task identity for the tests."""

    return TaskKey(server_id=server, session_id="sess-1", task_id=task_id)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """Every test starts and ends with no registered observer factories."""

    unregister_task_observer_factory(SERVER_A)
    unregister_task_observer_factory(SERVER_B)
    yield
    unregister_task_observer_factory(SERVER_A)
    unregister_task_observer_factory(SERVER_B)


# --------------------------------------------------------------------------- #
# The registry, in isolation                                                  #
# --------------------------------------------------------------------------- #


def test_unknown_server_id_resolves_to_none() -> None:
    """No factory registered for this server_id -- the documented default."""

    assert resolve_task_observer(_key("task-1", server="never-registered")) is None


def test_registered_factory_is_called_with_the_full_key_and_its_hook_returned() -> None:
    """register -> resolve returns exactly the hook the factory built for this key."""

    seen_keys: list[TaskKey] = []

    async def hook(current: Any, key: TaskKey, store: Any) -> None:
        """A stand-in on_poll hook."""

    def factory(key: TaskKey) -> Any:
        seen_keys.append(key)
        return hook

    register_task_observer_factory(SERVER_A, factory)
    key = _key("task-7")
    resolved = resolve_task_observer(key)

    assert resolved is hook
    assert seen_keys == [key]


def test_a_factory_returning_none_is_a_legitimate_answer() -> None:
    """A factory may decline to observe (e.g. console tailing disabled) -- no warning."""

    register_task_observer_factory(SERVER_A, lambda key: None)
    assert resolve_task_observer(_key("task-2")) is None


def test_unregister_reverts_to_the_unregistered_default() -> None:
    """After unregister, resolution behaves exactly as if never registered."""

    register_task_observer_factory(SERVER_A, lambda key: (lambda *_a: None))
    assert resolve_task_observer(_key("task-3")) is not None
    unregister_task_observer_factory(SERVER_A)
    assert resolve_task_observer(_key("task-3")) is None


def test_unregister_is_idempotent_on_an_unregistered_id() -> None:
    """Unregistering an id with nothing registered is a harmless no-op."""

    unregister_task_observer_factory("never-registered-anywhere")


def test_a_raising_factory_is_caught_and_degrades_to_none_with_a_typed_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken factory must never break the drive it was about to observe."""

    def broken_factory(key: TaskKey) -> Any:
        raise RuntimeError("boom")

    register_task_observer_factory(SERVER_A, broken_factory)
    with caplog.at_level(logging.WARNING, logger="clio_agent.tools.task_observers"):
        resolved = resolve_task_observer(_key("task-4"))

    assert resolved is None
    assert any(MCP_TASK_OBSERVER_FACTORY_FAILED in record.message for record in caplog.records)


def test_registration_is_scoped_per_server_id() -> None:
    """A factory registered for one backend never answers for another's tasks."""

    register_task_observer_factory(SERVER_A, lambda key: (lambda *_a: None))
    assert resolve_task_observer(_key("task-5", server=SERVER_B)) is None


def test_last_writer_wins_on_re_registration() -> None:
    """Registering twice for the same server_id replaces, not stacks."""

    register_task_observer_factory(SERVER_A, lambda key: "first")  # type: ignore[arg-type,return-value]
    register_task_observer_factory(SERVER_A, lambda key: "second")  # type: ignore[arg-type,return-value]
    assert resolve_task_observer(_key("task-6")) == "second"


# --------------------------------------------------------------------------- #
# The transparent extension actually calls it (the run-14 gap)                #
# --------------------------------------------------------------------------- #


def _task_payload(
    task_id: str,
    status: str,
    *,
    poll_interval_ms: float | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One ``tasks/get`` wire response (mirrors ``test_mcp_tasks.py``'s helper)."""

    payload: dict[str, Any] = {
        "taskId": task_id,
        "status": status,
        "createdAt": "2026-08-20T00:00:00+00:00",
        "lastUpdatedAt": "2026-08-20T00:00:00+00:00",
        "resultType": "complete",
    }
    if poll_interval_ms is not None:
        payload["pollIntervalMs"] = poll_interval_ms
    if result is not None:
        payload["result"] = result
    return payload


def _create_result(task_id: str) -> Any:
    """A claimed ``CreateTaskResult`` as a task-serving backend returns it."""

    from fastmcp_tasks.client_models import ClientCreateTaskResult

    return ClientCreateTaskResult.model_validate(
        {
            "taskId": task_id,
            "status": "working",
            "createdAt": "2026-08-20T00:00:00+00:00",
            "lastUpdatedAt": "2026-08-20T00:00:00+00:00",
            "resultType": "task",
        }
    )


class _ScriptedSession:
    """A minimal fake ``ClientSession`` answering ``tasks/get`` from a queued script."""

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self._script = list(script)
        self.requests: list[Any] = []

    async def send_request(
        self,
        request: Any,
        result_type: Any,
        request_read_timeout_seconds: float | None = None,
    ) -> Any:
        """Answer the next scripted ``tasks/get``."""

        self.requests.append(request)
        assert request.method == "tasks/get", f"unexpected task RPC {request.method!r}"
        return result_type.model_validate(self._script.pop(0))

    async def send_notification(self, notification: Any) -> None:
        """No-op: this scripted session never receives cancel notifications."""


class _Ctx:
    """Minimal ``ClaimContext`` stand-in (mirrors ``test_mcp_tasks.py``'s ``_Ctx``)."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.read_timeout_seconds: float | None = None


async def test_registered_observer_fires_on_every_poll_of_an_auto_claimed_task() -> None:
    """The run-14 gap, closed: a REGISTERED backend now folds through the transparent path."""

    store = InMemoryTaskRecordStore()
    set_task_record_store(store)
    observed: list[str] = []

    async def hook(current: Any, key: TaskKey, hook_store: Any) -> None:
        observed.append(current.status)

    register_task_observer_factory(SERVER_A, lambda key: hook)
    try:
        session = _ScriptedSession(
            [
                _task_payload("task-9", "working", poll_interval_ms=1),
                _task_payload(
                    "task-9",
                    "completed",
                    result={"content": [{"type": "text", "text": "ok"}]},
                ),
            ]
        )
        extension = ClioTasksClientExtension(BackendIdentity(SERVER_A, {"transport": "test"}))
        result = await extension._resolve_task(_create_result("task-9"), _Ctx(session))
    finally:
        set_task_record_store(None)

    assert result.content[0].text == "ok"
    # Once per OBSERVED poll, including the terminal one -- the exact contract
    # drive_task_to_terminal documents for on_poll.
    assert observed == ["working", "completed"]


async def test_unregistered_backend_drives_with_no_hook_identical_to_today() -> None:
    """No factory registered for this server_id -- behavior is unchanged (on_poll=None)."""

    store = InMemoryTaskRecordStore()
    set_task_record_store(store)
    try:
        session = _ScriptedSession(
            [
                _task_payload(
                    "task-10",
                    "completed",
                    result={"content": [{"type": "text", "text": "ok"}]},
                )
            ]
        )
        extension = ClioTasksClientExtension(BackendIdentity(SERVER_B, {"transport": "test"}))
        result = await extension._resolve_task(_create_result("task-10"), _Ctx(session))
    finally:
        set_task_record_store(None)

    assert result.content[0].text == "ok"


async def test_a_broken_registered_factory_never_breaks_the_drive() -> None:
    """The registry's own guard: a raising factory degrades, the task still completes."""

    store = InMemoryTaskRecordStore()
    set_task_record_store(store)

    def broken_factory(key: TaskKey) -> Any:
        raise RuntimeError("factory blew up")

    register_task_observer_factory(SERVER_A, broken_factory)
    try:
        session = _ScriptedSession(
            [
                _task_payload(
                    "task-11",
                    "completed",
                    result={"content": [{"type": "text", "text": "ok"}]},
                )
            ]
        )
        extension = ClioTasksClientExtension(BackendIdentity(SERVER_A, {"transport": "test"}))
        result = await extension._resolve_task(_create_result("task-11"), _Ctx(session))
    finally:
        set_task_record_store(None)

    assert result.content[0].text == "ok"
