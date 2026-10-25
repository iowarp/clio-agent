"""REST acceptance returns the durable shared identity of real storage work."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.storage.models import FileEntry, TransferOperation
from clio_agent.platform_paths import win_extended_path

pytestmark = pytest.mark.usefixtures("host_agent_executor")


def _await_chain(coroutine: Any) -> list[str]:
    """Describe suspended asyncio owners without advancing or cancelling them."""
    chain: list[str] = []
    seen: set[int] = set()
    while coroutine is not None and id(coroutine) not in seen:
        seen.add(id(coroutine))
        frame = (
            getattr(coroutine, "cr_frame", None)
            or getattr(coroutine, "gi_frame", None)
            or getattr(coroutine, "ag_frame", None)
        )
        if frame is None:
            chain.append(type(coroutine).__name__)
        else:
            chain.append(f"{frame.f_code.co_filename}:{frame.f_lineno}:{frame.f_code.co_name}")
        coroutine = (
            getattr(coroutine, "cr_await", None)
            or getattr(coroutine, "gi_yieldfrom", None)
            or getattr(coroutine, "ag_await", None)
        )
    return chain


def _diagnose_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep real shutdown behavior and capture its async owners before the hard guard."""
    from clio_agent.gact import task_runtime
    from tests import _hang_guard

    original_close = task_runtime.close

    async def describe(app: Any) -> None:
        while True:
            await asyncio.sleep(20)  # Diagnostic sampling only; the 120s test limit is unchanged.
            native = app.state.native_tools
            history = app.state.task_history
            calls = []
            for handle in tuple(native._calls):
                current = history.task_snapshot(handle)
                calls.append(
                    {
                        "handle": handle,
                        "status": current.status if current else None,
                        "raw_status": current.raw_status if current else None,
                        "settlement": current.settlement if current else None,
                        "terminal_at": current.terminal_at if current else None,
                        "attempts": [
                            {
                                "incarnation": attempt.incarnation,
                                "cleanup_state": attempt.cleanup_state,
                                "cleanup_receipt": str(attempt.cleanup_receipt),
                                "finished_at": attempt.finished_at,
                            }
                            for attempt in tuple(history._attempts.get(handle, {}).values())
                        ],
                    }
                )
            diagnostic = {
                "calls": calls,
                "recovery_pending": app.state.task_recovery.pending,
                "drivers": tuple(native._drivers),
                "rpcs": tuple(native._rpc),
                "tasks": [
                    {"name": task.get_name(), "await_chain": _await_chain(task.get_coro())}
                    for task in asyncio.all_tasks()
                ],
            }
            # Use the hang guard's stream so xdist attaches this to a killed worker's report.
            stream = _hang_guard._dump_stream or sys.stderr
            print("Storage shutdown owners: " + json.dumps(diagnostic, default=str), file=stream)
            stream.flush()

    async def observed_close(app: Any) -> tuple[BaseException, ...]:
        diagnostic = asyncio.create_task(describe(app), name="storage-shutdown-diagnostic")
        try:
            return await original_close(app)
        finally:
            diagnostic.cancel()
            await asyncio.gather(diagnostic, return_exceptions=True)

    monkeypatch.setattr(task_runtime, "close", observed_close)


def _storage_result(client: TestClient, sid: str, handle: str) -> dict[str, Any]:
    """Read the bounded public result pages, then decode the stored native result."""
    endpoint = f"/v1/sessions/{sid}/tasks/{handle}/result"
    deadline = time.monotonic() + 10
    while True:
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        page = response.json()
        if not page["pending"]:
            break
        assert time.monotonic() < deadline, page
        time.sleep(0.01)
    assert page["status"] == "completed", page
    if page.get("fragments_require_concatenation"):
        reference, parts = page["result_reference"], [page["result"]["preview"]]
        while page["next_cursor"]:
            previous_offset = page["next_offset"]
            response = client.get(endpoint, params={"cursor": page["next_cursor"]})
            assert response.status_code == 200, response.text
            page = response.json()
            assert page["result_reference"] == reference and page["offset"] == previous_offset
            parts.append(page["result"]["preview"])
        raw = json.loads("".join(parts))
    else:
        raw = page["result"]
    assert raw["result"]["isError"] is False, raw
    value = raw["result"]["structuredContent"]
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("action,kind", [("link", "Indexing"), ("transfer", "Download")])
@pytest.mark.timeout(120)  # Actual Core and native storage lifespans must settle.
def test_storage_http_ack_contains_persisted_queryable_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, kind: str
) -> None:
    """Join the actual 202 response, owner ledger, shared query and terminal result."""
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setenv("CLIO_TOOL_RESULT_CHARS", "512")
    upstream, workspace = tmp_path / "upstream", tmp_path / "workspace"
    upstream.mkdir()
    workspace.mkdir()
    payload = b"actual REST storage acceptance\n"
    (upstream / "payload.txt").write_bytes(payload)
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        created = client.post(
            "/v1/workspaces", json={"name": "Task receipt", "root_path": str(workspace)}
        )
        assert created.status_code == 201, created.text
        wid = created.json()["id"]
        session = client.post("/v1/sessions", json={"workspace_id": wid, "title": "owner"})
        assert session.status_code == 200, session.text
        sid = session.json()["id"]
        source = client.post(
            f"/v1/workspaces/{wid}/sources",
            json={"provider": "local", "root": str(upstream), "label": "Owned data"},
        )
        assert source.status_code == 201, source.text
        prefix = f"/v1/workspaces/{wid}/sources/{source.json()['id']}"
        accepted = client.post(prefix + f"/{action}", json={"session_id": sid})
        assert accepted.status_code == 202, accepted.text
        receipt = accepted.json()
        operation = receipt["indexing_operation"] if action == "link" else receipt
        handle = operation["task_handle"]
        assert isinstance(handle, str) and handle.startswith("task_")
        persisted = app.state.task_history.task_snapshot(handle)
        assert persisted is not None and persisted.handle == handle
        assert persisted.owner.session_id == sid and persisted.recording == "confirmed"
        assert persisted.submission is not None
        snapshot = client.get(f"/v1/sessions/{sid}/tasks/query", params={"handle": handle})
        assert snapshot.status_code == 200, snapshot.text
        task = snapshot.json()["tasks"]
        assert len(task) == 1 and task[0]["kind"] == kind
        assert task[0]["owner"]["session_id"] == sid
        result = _storage_result(client, sid, handle)
        assert result["operation_id"] == handle and result["state"] == "completed"
        resource_operation = app.state.connected_storage.store.get(
            "operation", operation["id"], TransferOperation
        )
        assert (
            resource_operation.id == handle and resource_operation.source_id == source.json()["id"]
        )
        assert persisted.resource_id == source.json()["id"]
        if kind == "Download":
            record = app.state.connected_storage.get(wid, source.json()["id"])
            assert record.source.local_path is not None
            local = Path(record.source.local_path) / "payload.txt"
            actual = Path(win_extended_path(local)).read_bytes()
            assert actual == payload
            assert hashlib.sha256(actual).digest() == hashlib.sha256(payload).digest()
            assert result["bytes_done"] == result["bytes_total"] == len(payload)
            assert result["hashes"]["payload.txt"] == hashlib.sha256(payload).hexdigest()
        else:
            assert result["entries"] == resource_operation.entries_done == 1


@pytest.mark.timeout(120)  # Held actual enumeration and queued cancellation require cleanup.
def test_queued_storage_is_visible_and_cancellable_before_worker_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The REST surface controls Core-reserved work even before a resource operation exists."""
    from clio_agent.gact.storage.linked import FsspecFolder

    _diagnose_shutdown(monkeypatch)
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent"))
    monkeypatch.setenv("CLIO_TASKS_STORAGE_CONCURRENCY", "1")
    entered, release = threading.Event(), threading.Event()
    original = FsspecFolder.entries

    def held(folder: FsspecFolder, *args: Any, **kwargs: Any) -> list[FileEntry]:
        entered.set()
        assert release.wait(10), "Test must release its actual enumeration worker"
        return original(folder, *args, **kwargs)

    monkeypatch.setattr(FsspecFolder, "entries", held)
    upstream, workspace = tmp_path / "upstream", tmp_path / "workspace"
    upstream.mkdir()
    workspace.mkdir()
    (upstream / "input.txt").write_text("owned input", encoding="utf-8")
    app = build_app(sessions_path=tmp_path / "sessions.json")
    with TestClient(app) as client:
        wid = client.post(
            "/v1/workspaces", json={"name": "Queued storage", "root_path": str(workspace)}
        ).json()["id"]
        sid = client.post("/v1/sessions", json={"workspace_id": wid}).json()["id"]
        prefixes = []
        for index, label in enumerate(("Blocking index", "Queued download")):
            source_root = upstream / str(index)
            source_root.mkdir()
            (source_root / "input.txt").write_text("owned input", encoding="utf-8")
            source = client.post(
                f"/v1/workspaces/{wid}/sources",
                json={"provider": "local", "root": str(source_root), "label": label},
            )
            assert source.status_code == 201, source.text
            prefixes.append(f"/v1/workspaces/{wid}/sources/{source.json()['id']}")
        try:
            first = client.post(prefixes[0] + "/link", json={"session_id": sid})
            assert first.status_code == 202, first.text
            assert entered.wait(5)
            queued = client.post(prefixes[1] + "/transfer", json={"session_id": sid})
            assert queued.status_code == 202, queued.text
            handle = queued.json()["task_handle"]
            history = app.state.task_history
            assert history.native_attempts(handle) == ()
            with pytest.raises(KeyError):
                app.state.connected_storage.store.get("operation", handle, TransferOperation)
            visible = client.get(prefixes[1] + "/operations").json()["operations"]
            assert len(visible) == 1 and visible[0]["task_handle"] == handle
            assert client.post(prefixes[0] + f"/operations/{handle}/cancel").status_code == 404, (
                "A handle from another source cannot be controlled through this source"
            )
            cancelled = client.post(prefixes[1] + f"/operations/{handle}/cancel")
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["cancel_requested"]
            deadline = time.monotonic() + 5
            while (current := history.task_snapshot(handle)) is None or current.terminal_at is None:
                assert time.monotonic() < deadline, current
                time.sleep(0.01)
            assert current.status == "cancelled" and current.settlement == "never_started"
            assert history.native_attempts(handle) == ()
            with pytest.raises(KeyError):
                app.state.connected_storage.store.get("operation", handle, TransferOperation)
        finally:
            release.set()
