"""Shared controls against the real session/task stores and event bus."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context
from clio_agent.gact.agent_tasks import AgentTaskRegistry, seed_agent_task
from clio_agent.gact.events import Event, EventBus
from clio_agent.gact.mcp_task_store import SessionMetadataTaskStore
from clio_agent.gact.sessions import SessionStore
from clio_agent.gact.task_controls import observe_tasks, query_snapshot, wait_tasks
from clio_agent.gact.task_delivery import consume_task, pending_completions
from clio_agent.gact.task_projection import resolve_task
from clio_agent.tools.mcp_task_records import TaskKey, TaskRecord


@pytest.fixture
def scoped_app(tmp_path: Path) -> Iterator[tuple[Any, str, SessionMetadataTaskStore]]:
    sessions = SessionStore(path=tmp_path / "sessions.json")
    sid = sessions.create(workspace_id="ws", title="parent").id
    store = SessionMetadataTaskStore(sessions)
    app = SimpleNamespace(
        state=SimpleNamespace(
            sessions=sessions,
            agent_task_registry=AgentTaskRegistry(),
            mcp_task_store=store,
            bus=EventBus(),
            cancel_flags=set(),
        )
    )
    app_token, sid_token = context.set_app(app), context.set_session_id(sid)
    try:
        yield app, sid, store
    finally:
        context.reset(sid_token)
        context.reset(app_token)


def record(
    store: SessionMetadataTaskStore,
    sid: str,
    handle: str,
    *,
    server: str = "a",
    task_id: str = "same",
    kind: str = "MCP",
    status: str = "working",
) -> TaskRecord:
    row = TaskRecord(
        key=TaskKey(server, sid, task_id),
        handle=handle,
        kind=kind,
        description="Read the requested document",
        status=status,
        result={"content": "real stored result"} if status == "completed" else None,
        notify_pending=status == "completed",
        created_at=handle,
    )
    store.put(row)
    return row


def test_composite_alias_collision_does_not_hide_or_collect_a_member(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
) -> None:
    app, sid, store = scoped_app
    record(store, sid, "first", server="a", status="completed")
    record(store, sid, "second", server="b", status="completed")
    with pytest.raises(ValueError, match="ambiguous_task"):
        resolve_task(app, sid, "same")
    outcome = wait_tasks(["same", "first", "second"], timeout_s=0)
    assert len(outcome["results"]) == 2
    assert outcome["errors"] == [{"handle": "same", "error": "ambiguous_task"}]
    assert {r["handle"] for r in outcome["results"]} == {"first", "second"}


def test_backend_sessions_and_stale_callback_keep_custody_and_delivery_once(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
) -> None:
    app, sid, store = scoped_app
    first = TaskRecord(
        key=TaskKey("backend", sid, "id", "transport-one"),
        handle="one",
        status="completed",
        result={"text": "first"},
        notify_pending=True,
    )
    second = replace(first, key=TaskKey("backend", sid, "id", "transport-two"), handle="two")
    store.put(first)
    store.put(second)
    assert first.key.row_key != second.key.row_key
    assert query_snapshot(app, sid)["total"] == 2
    assert consume_task(app, sid, "one")
    store.put(replace(first, notify_pending=True, cancel_requested=True))
    assert store.get(first.key).consumed_at
    assert not store.get(first.key).notify_pending
    assert not consume_task(app, sid, "one")
    assert pending_completions(app, sid)[1] == ["two"]


def test_collection_between_staging_and_commit_cannot_duplicate_injection(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
) -> None:
    from clio_agent.gact.task_delivery import commit_staged_completions

    app, sid, store = scoped_app
    record(store, sid, "staged", status="completed")
    block, handles = pending_completions(app, sid)
    state = SimpleNamespace(
        app=app,
        sid=sid,
        pending_notification_task_ids=handles,
        injections=[("todos", "keep"), ("task_results", block)],
    )
    assert wait_tasks("staged", timeout_s=0)["results"]
    commit_staged_completions(state)
    assert state.injections == [("todos", "keep")]


def test_background_shell_keeps_tree_ownership_after_shell_exit_and_cancels_descendant(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
    tmp_path: Path,
) -> None:
    import asyncio
    import sys

    import psutil

    from clio_agent.gact.task_shell_owner import accept_shell
    from clio_agent.gact.task_supervisor import task_supervisor
    from clio_agent.runtime.task_process_tree import task_spawn_options
    from clio_agent.tools.servers.shell_output import StreamCapture

    app, sid, store = scoped_app
    pid_file = tmp_path / "owned-child.pid"
    code = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); print('owned parent exited',flush=True)"
    )

    async def exercise() -> None:
        app.state.mcp_app_loop = asyncio.get_running_loop()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **task_spawn_options({}),
        )
        supervisor = task_supervisor(app)
        captures = [
            StreamCapture(stream, inline_limit=4096, spill_path=tmp_path / f"{stream}.log")
            for stream in ("stdout", "stderr")
        ]
        accepted = accept_shell(
            app,
            sid,
            process,
            captures,
            command="owned lifecycle case",
            cwd=tmp_path,
            spill_root=tmp_path,
            timeout=0,
            invocation_id="test-call",
        )
        handle = accepted["handle"]
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            child_pid = int(pid_file.read_text())
            assert psutil.pid_exists(child_pid)
            assert not supervisor.drivers[handle].done()
            row = resolve_task(app, sid, handle)
            assert row["effective_status"] == "running"
            assert await asyncio.to_thread(supervisor.request_cancel, row)
            driver = supervisor.drivers.get(handle)
            if driver is not None:
                await asyncio.wait_for(asyncio.shield(driver), timeout=5)
            assert not psutil.pid_exists(child_pid)
            settled = resolve_task(app, sid, handle)
            assert settled["effective_status"] == "cancelled"
            assert "owned parent exited" in settled["result"]["stdout"]
            assert settled["invocation_id"] == "test-call"
        finally:
            await supervisor.shutdown()

    asyncio.run(exercise())


def test_authorization_precedes_filters_pagination_and_counts(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
) -> None:
    app, sid, store = scoped_app
    child = seed_agent_task(
        app, parent_session_id=sid, agent_ref={"expert_id": "child"}, status="running"
    )
    sibling = seed_agent_task(
        app, parent_session_id=sid, agent_ref={"expert_id": "sibling"}, status="running"
    )
    record(store, sid, "ancestor")
    record(store, child.child_session_id, "own")
    record(store, sibling.child_session_id, "sibling")
    stranger = app.state.sessions.create(workspace_id="ws", title="unrelated").id
    record(store, stranger, "unrelated")
    page = query_snapshot(app, child.child_session_id, kind="MCP", limit=1)
    assert page["total"] == 1 and page["cursor"] is None
    assert [r["handle"] for r in page["tasks"]] == ["own"]
    with pytest.raises(ValueError, match="unknown_or_unauthorized"):
        resolve_task(app, child.child_session_id, "ancestor")


def test_query_pages_survive_new_insertion_without_duplicates(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
) -> None:
    app, sid, store = scoped_app
    for handle in ("a", "b", "c"):
        record(store, sid, handle, task_id=handle)
    first = query_snapshot(app, sid, limit=1)
    record(store, sid, "d", task_id="d")
    second = query_snapshot(app, sid, limit=1, cursor=first["cursor"])
    assert first["tasks"][0]["handle"] == "c"
    assert second["tasks"][0]["handle"] == "b"
    with pytest.raises(ValueError, match="invalid task cursor"):
        query_snapshot(app, sid, kind="Shell", cursor=first["cursor"])


def test_expired_mixed_wait_and_invalid_member_leave_work_running(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
) -> None:
    _app, sid, store = scoped_app
    active = record(store, sid, "shell", kind="Shell")
    record(store, sid, "download", server="b", kind="Download", status="completed")
    result = wait_tasks(["shell", "download", "invalid"], timeout_s=0)
    assert result["pending"] == ["shell"] and result["timed_out"]
    assert result["results"][0]["kind"] == "Download"
    assert not store.get(active.key).cancel_requested
    assert store.get(active.key).status == "working"


def test_stop_interrupts_waiter_without_changing_accepted_task(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
) -> None:
    app, sid, store = scoped_app
    active = record(store, sid, "accepted")
    app.state.cancel_flags.add(sid)
    with pytest.raises(InterruptedError, match="accepted tasks continue"):
        wait_tasks("accepted")
    assert store.get(active.key).status == "working"
    assert not store.get(active.key).cancel_requested


def test_query_and_observe_do_not_consume_pending_completion(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
) -> None:
    app, sid, store = scoped_app
    row = record(store, sid, "done", status="completed")
    query_snapshot(app, sid)
    observed = observe_tasks("done")
    assert observed["tasks"][0]["handle"] == "done"
    assert store.get(row.key).notify_pending
    wait_tasks("done", timeout_s=0)
    assert not store.get(row.key).notify_pending
    assert (
        wait_tasks("done", timeout_s=0)["results"][0]["result"]["content"] == "real stored result"
    )


def test_staging_veto_overflow_and_concurrent_collection_share_guard(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
) -> None:
    app, sid, store = scoped_app
    for i in range(10):
        record(store, sid, f"h{i}", task_id=f"t{i}", status="completed")
    block, staged = pending_completions(app, sid)
    assert len(staged) == 8 and "2 additional" in block
    assert pending_completions(app, sid)[1] == staged  # veto does not consume
    claims: list[bool] = []
    threads = [
        threading.Thread(target=lambda: claims.append(consume_task(app, sid, staged[0])))
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert claims.count(True) == 1
    for handle in staged[1:]:
        consume_task(app, sid, handle)
    assert len(pending_completions(app, sid)[1]) == 2


def test_handle_and_result_survive_store_reconstruction(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
) -> None:
    app, sid, store = scoped_app
    row = record(store, sid, "durable", status="completed")
    rebuilt = SessionMetadataTaskStore(SessionStore(path=app.state.sessions._path))
    restored = rebuilt.get(row.key)
    assert restored.handle == "durable"
    assert restored.description == row.description and restored.result == row.result


def test_observation_cursor_is_incremental_and_recovers_after_restart(
    scoped_app: tuple[Any, str, SessionMetadataTaskStore],
) -> None:
    app, sid, store = scoped_app
    row = record(store, sid, "output")
    app.state.bus.publish(
        Event(type="task.output", session_id=sid, payload={"handle": "output", "text": "first"})
    )
    first = observe_tasks("output")
    app.state.bus.publish(
        Event(type="task.output", session_id=sid, payload={"handle": "output", "text": "second"})
    )
    second = observe_tasks("output", cursor=first["cursor"])
    assert [e["payload"]["text"] for e in second["events"]] == ["second"]
    store.put(replace(row, status="completed", result={"text": "done"}, notify_pending=True))
    assert observe_tasks("output", cursor=10**15)["cursor_recovered"]
