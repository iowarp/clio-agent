"""Recovery of a read-only call's record written behind its reply (read_only_outcomes).

Two risks of writing the record behind the reply, group-committed:

* **Partial landing.** A group flush puts content, then the task-event lane, then the
  ``_events`` log. When a later rank fails, the earlier ones already hold the record;
  recovery retries each operation alone. Every reader must still see each event once:
  the task tree (``task_snapshot``, ``query_tasks``, ``observe_tasks``), the lane, and
  the ``_events`` log that export, trace and provenance read.
* **Crash window.** A record queued but not yet written is lost with the process.
  Nothing is resubmitted or resurrected; the turn's own record of the call is
  reported as unknown.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client, FastMCP
from mcp.types import ToolAnnotations

from clio_agent.arc.batch_put import BatchPutError
from clio_agent.arc.memory import ARCMemory
from clio_agent.gact.external_tasks import ExternalTasks
from clio_agent.gact.read_only_outcomes import READ_ONLY_OUTCOME_UNRECORDED
from clio_agent.gact.session_export import _events as exported_events
from clio_agent.gact.task_access import TaskCaller
from clio_agent.gact.task_event_lane import session_task_events
from clio_agent.gact.task_event_reconcile import TASK_EVENT_RECONCILED
from clio_agent.gact.task_operations import TaskOperations
from tests.test_arc.test_external_read_only_admission import _environment
from tests.test_arc.test_read_only_outcomes import _Calls, _close, _mcp_tasks, _setup

_TASK_EVENTS = ("task.submission", "task.observed")


def _failing_rank(store: Any, monkeypatch: pytest.MonkeyPatch, rank: str) -> dict[str, bool]:
    """Fail the next segments ``put_many`` of ``rank`` once: the lane or the log."""
    original = store.put_many
    armed = {"on": False}

    def scope_of(name: str) -> str:
        return name.split("__", 1)[1]

    def hits(names: list[str]) -> bool:
        if rank == "lane":
            return any(scope_of(n).startswith("_events~t~log") for n in names)
        return any(scope_of(n) == "_events" for n in names)

    def put_many(kind: str, records: Any) -> None:
        records = list(records)
        if armed["on"] and kind == "segments" and hits([r.name for r in records]):
            armed["on"] = False
            raise BatchPutError(kind, [], {r.name: RuntimeError("lost") for r in records})
        original(kind, records)

    monkeypatch.setattr(store, "put_many", put_many)
    return armed


def _ours(contents: list[dict[str, Any]], handle: str, reference: str) -> list[tuple[str, str]]:
    """``(event type, identity)`` of the call's task events, in record order."""
    out = []
    for content in contents:
        if content.get("event_type") not in _TASK_EVENTS:
            continue
        payload = content["payload"]
        record = payload["record"]
        if record.get("handle") == handle or record.get("reference") == reference:
            out.append((str(content["event_type"]), str(payload["observation_id"])))
    return out


@pytest.mark.asyncio
@pytest.mark.timeout(180)  # in-memory MCP sessions, a failed group and a recovery cycle
@pytest.mark.parametrize("built", [True, False], ids=["built-index", "before-index-build"])
@pytest.mark.parametrize("rank", ["log", "lane"])
async def test_a_partly_landed_group_is_recorded_once_for_every_reader(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    rank: str,
    built: bool,
) -> None:
    """A group's ``_events`` put (or its lane put) fails once, after the ranks below it
    landed. After recovery the call's submission and settled snapshot are each held
    once in the lane and once in ``_events``, with one identity; the task tree, query,
    observe and export all report one completed call; nothing is logged unrecorded.

    SABOTAGE: run the retry without ``reconciled`` in ``ReadOnlyOutcomes._finished``
    (lane landed: the built index refuses the re-derived outcome and ``_events`` never
    gets it; before the build the lane gets a second submission and snapshot); or drop
    the fixed ``observation``/``at`` from ``ExternalTasks._call`` (the retry is a
    different observation).
    """
    app, external, calls, release = await _setup(arc, tmp_path)
    history = app.state.task_history
    sid = calls.root.owner.session_id
    if built:  # as a server runs: past the one-time index build, trees read the lane
        await history.io.run(history.restore)
        await history.io.run(history.complete_index_build)
    armed = _failing_rank(arc._store, monkeypatch, rank)
    gate = threading.Event()
    try:
        await calls("read", "R0")
        await external.outcomes.recorded()
        history.io.submit(gate.wait, 30)  # two records queue: one group
        armed["on"] = True
        replies = await asyncio.gather(calls("read", "P1"), calls("read", "P2"))
        assert [reply.structured_content for reply in replies] == [{"n": "1"}, {"n": "2"}]
        with caplog.at_level(logging.WARNING):
            gate.set()
            await external.outcomes.recorded()
            assert not armed["on"] and history.requires_recovery
            await app.state.task_recovery.drain()
        assert not history.requires_recovery
        assert not app.state.task_recovery.pending
        assert f"reason={READ_ONLY_OUTCOME_UNRECORDED}" not in caplog.text
        if rank == "log":  # the lane held them: only the log was completed
            assert f"reason={TASK_EVENT_RECONCILED}" in caplog.text
    finally:
        gate.set()
        await _close(app, external, release)

    def readers() -> dict[str, Any]:
        arc._segments.release(sid)  # clio-core's records, not the writer's cache
        history.restore()
        tasks = _mcp_tasks(app, sid)
        references = {
            key: str(history.read_content(task.submission)["reference"])
            for key, task in tasks.items()
        }
        lane = [content for _sid, content in session_task_events(arc, sid)]
        log = [segment.content for segment in arc._live.iter_session_event_segments(sid)]
        exported, _source = exported_events(app, sid)
        return {
            "tasks": tasks,
            "references": references,
            "lane": lane,
            "log": log,
            "exported": exported,
        }

    seen = await asyncio.to_thread(readers)
    tasks = seen["tasks"]
    assert set(tasks) == {"R0", "P1", "P2"}

    async def unused_cancel(_task: Any, _caller: TaskCaller) -> dict[str, Any]:
        pytest.fail("reading tasks never cancels")

    operations = TaskOperations(history, unused_cancel)
    caller = TaskCaller(sid, "human")
    for key in ("P1", "P2"):
        task = tasks[key]
        handle, reference = task.handle, seen["references"][key]
        lane = _ours(seen["lane"], handle, reference)
        log = _ours(seen["log"], handle, reference)
        exported = _ours(seen["exported"], handle, reference)
        assert [event for event, _ in lane] == list(_TASK_EVENTS), (key, lane)
        assert log == lane and exported == lane, (key, lane, log, exported)
        observed = next(
            content["payload"]["record"]
            for content in seen["lane"]
            if content.get("event_type") == "task.observed"
            and content["payload"]["record"].get("handle") == handle
        )
        assert task.status == "completed" and task.raw_status == "inline"
        assert observed["terminal_at"] == task.terminal_at
        result = await asyncio.to_thread(history.read_content, task.result)
        assert result["structuredContent"] == {"n": key[1:]}
        page = await asyncio.to_thread(operations.query, caller, handle=handle)
        rows = [row for row in page["tasks"] if row["handle"] == handle]
        assert len(rows) == 1 and rows[0]["status"] == "completed", page
        observation = await operations.observe(caller, handle, timeout_s=0)
        rows = [row for row in observation["tasks"] if row["handle"] == handle]
        assert len(rows) == 1 and rows[0]["status"] == "completed", observation
        assert observation["any_settled"], observation


def _counting_server(release: asyncio.Event, started: asyncio.Event, sent: list[str]) -> Any:
    server = FastMCP("records")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def read(record_id: str) -> dict[str, str]:
        sent.append(record_id)
        return {"n": record_id[1:]}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def write(record_id: str) -> dict[str, str]:
        sent.append(record_id)
        started.set()
        await release.wait()
        return {"n": record_id[1:]}

    return server


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_record_lost_in_the_queue_is_not_resurrected_or_resubmitted(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The read-only call answers and its record is queued on the history writer, which
    never runs it (the process dies). A restored history and a fresh owner hold no task,
    index row or task event for it, nothing is pending recovery, and the server is not
    called again; the write call in flight, recorded before it was sent, is reported
    uncertain.

    SABOTAGE: make ``skips_write_ahead`` return False (the read is recorded before it is
    sent and survives as a task); or have ``ReadOnlyOutcomes.record`` run its write on
    the writer before queuing (the "lost" record lands and the task is resurrected).
    """
    app, root = await _environment(arc, tmp_path)
    history = app.state.task_history
    sid = root.owner.session_id
    await history.io.run(history.restore)
    await history.io.run(history.complete_index_build)
    release, started, sent = asyncio.Event(), asyncio.Event(), []
    external = ExternalTasks(app)
    calls = _Calls(app, root, external, Client(_counting_server(release, started, sent)))
    queued: list[Any] = []
    real_batch = history.io.submit_batch
    writing: asyncio.Task[Any] | None = None
    try:
        await calls("read", "R0")
        await external.outcomes.recorded()
        monkeypatch.setattr(history.io, "submit_batch", lambda _runner, item: queued.append(item))
        reply = await calls("read", "C1")  # answered; its record waits in the queue
        assert not reply.is_error and reply.structured_content == {"n": "1"}
        assert [item.identity for item in queued] and external.outcomes.pending == 1
        lost = queued[0].identity.removeprefix("external-inline:")
        writing = asyncio.create_task(calls("write", "W1"))
        await asyncio.wait_for(started.wait(), 15)

        # The restart: a fresh owner over clio-core's records; the queue is gone.
        monkeypatch.setattr(history.io, "submit_batch", real_batch)

        def restored() -> None:
            arc._segments.release(sid)
            history.restore()

        await history.io.run(restored)
        survivor = ExternalTasks(app)
        await survivor.restore()
        await survivor.close()
        await app.state.task_recovery.drain()

        tasks = await asyncio.to_thread(_mcp_tasks, app, sid)
        assert set(tasks) == {"R0", "W1"}
        assert history.index.handle(lost) is None
        assert tasks["W1"].status == "task_state_uncertain"
        assert "nothing was resubmitted" in tasks["W1"].progress.message
        assert not any(lost in pending for pending in app.state.task_recovery.pending)
        assert sent == ["R0", "C1", "W1"]  # nothing went to the server again

        def events() -> list[Any]:
            lane = [content for _sid, content in session_task_events(arc, sid)]
            log = [segment.content for segment in arc._live.iter_session_event_segments(sid)]
            return [
                content
                for content in lane + log
                if content.get("event_type") in _TASK_EVENTS
                and content["payload"]["record"].get("handle") == lost
            ]

        assert await asyncio.to_thread(events) == []
    finally:
        for item in queued:  # what the dead process held; it never resolves
            item.done.cancel()
        release.set()
        if writing is not None:
            await asyncio.gather(writing, return_exceptions=True)
        await external.close()
        await app.state.task_recovery.drain()
