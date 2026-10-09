"""Recovery reconstructs original HTTP custody without submitting work again."""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.client.transports import StreamableHttpTransport

from clio_agent.gact.task_recovery import recover_http
from clio_agent.tools.mcp_task_extension import backend_identity
from clio_agent.tools.mcp_task_records import (
    InMemoryTaskRecordStore,
    TaskKey,
    TaskLease,
    TaskRecord,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("held_lease", [False, True], ids=["free", "held"])
async def test_http_recovery_rebuilds_transport_and_resumes_full_original_identity(
    monkeypatch: pytest.MonkeyPatch,
    held_lease: bool,
) -> None:
    """An unconfigured pack-owned backend still retains its approved transport type."""
    identity = backend_identity(StreamableHttpTransport("http://127.0.0.1:18836/mcp"))
    key = TaskKey(identity.server_id, "conversation", "same-task", "original-backend-session")
    store = InMemoryTaskRecordStore()
    negotiation = {
        "type": "initialize_result",
        "result": {
            "protocolVersion": "2026-07-28",
            "capabilities": {},
            "serverInfo": {"name": "production-shape", "version": "1"},
        },
    }
    store.put(
        TaskRecord(
            key=key,
            handle="task_original",
            tool="fetch",
            backend={**identity.locator, "negotiation": negotiation},
            status="working",
        )
    )
    seen: dict[str, Any] = {}
    app = SimpleNamespace(state=SimpleNamespace(external_mcp_servers={}))
    monkeypatch.setattr("clio_agent.gact.task_recovery.app_task_store", lambda _: store)

    @asynccontextmanager
    async def connect(_app: Any, transport: Any, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        assert isinstance(transport, StreamableHttpTransport)
        assert transport.headers["mcp-session-id"] == key.backend_session_id
        seen["transport"] = backend_identity(transport)
        if held_lease:
            row = store.get(key)
            assert row is not None
            store.put(
                replace(
                    row,
                    lease_owner="previous-driver",
                    lease_expires_at=time.time() + 0.05,
                )
            )
        yield SimpleNamespace(
            session=SimpleNamespace(adopt=lambda result: seen.update(adopt=result))
        )

    async def resume(_session: Any, resumed: TaskKey, **_kwargs: Any) -> None:
        with TaskLease(store, resumed):
            seen["key"] = resumed
            row = store.get(resumed)
            assert row is not None
            store.put(replace(row, status="completed", result={"content": []}))

    monkeypatch.setattr("clio_agent.gact.elicitation_bridge.make_elicitation_client", connect)
    monkeypatch.setattr("clio_agent.tools.mcp_tasks.resume_task", resume)
    await recover_http(app, key)
    retained = store.get(key)
    assert retained is not None and retained.status == "completed"
    assert seen["key"] == key and seen["transport"] == identity
    assert (
        seen["adopt"].model_dump(by_alias=True, exclude_none=True)["serverInfo"]
        == negotiation["result"]["serverInfo"]
    )
