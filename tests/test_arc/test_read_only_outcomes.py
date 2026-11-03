"""A read-only MCP call's record is written behind its reply, in group commits.

Measured on a P7-shaped burst (2026-11-03): each read-only call held its in-flight slot
until about 13 clio-core round trips were confirmed on the single history writer. The
reply now returns at once; the record is queued on that writer (FIFO), several queued
records are confirmed together, and a failed record is typed and goes to recovery.
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
from clio_agent.gact import context
from clio_agent.gact.external_tasks import ExternalTasks
from clio_agent.gact.read_only_outcomes import READ_ONLY_OUTCOME_DEFERRED
from clio_agent.gact.task_event_lane import session_task_events
from clio_agent.tools.task_call_context import TASK_CALL, TaskCall
from tests.test_arc.test_external_read_only_admission import _environment


def _server(release: asyncio.Event, started: asyncio.Event) -> FastMCP:
    server = FastMCP("records")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def read(record_id: str) -> dict[str, str]:
        return {"n": record_id[1:]}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
    async def write(record_id: str) -> dict[str, str]:
        started.set()
        await release.wait()
        return {"n": record_id[1:]}

    return server


class _Calls:
    def __init__(self, app: Any, root: Any, external: ExternalTasks, client: Client) -> None:
        self.app, self.root, self.external, self.client = app, root, external, client

    async def __call__(self, tool: str, record_id: str) -> Any:
        tokens = (context.set_app(self.app), context.set_session_id(self.root.owner.session_id))
        invocation = TASK_CALL.set(TaskCall(invocation_id="inv-" + record_id))
        try:
            return await self.external.call(
                "records", self.client, tool, {"record_id": record_id}, auth_scope="trusted-scope"
            )
        finally:
            TASK_CALL.reset(invocation)
            for token in reversed(tokens):
                context.reset(token)


async def _setup(
    arc: ARCMemory, tmp_path: Path
) -> tuple[Any, ExternalTasks, _Calls, asyncio.Event]:
    app, root = await _environment(arc, tmp_path)
    external = ExternalTasks(app)
    release, started = asyncio.Event(), asyncio.Event()
    calls = _Calls(app, root, external, Client(_server(release, started)))
    calls.started = started  # type: ignore[attr-defined]
    return app, external, calls, release


def _mcp_tasks(app: Any, sid: str) -> dict[str, Any]:
    """The session's MCP tasks by record id (view reads; never on the history writer)."""
    history = app.state.task_history
    out: dict[str, Any] = {}
    for handle in history.task_handles(session_id=sid):
        task = history.task_snapshot(handle)
        if task is not None and task.kind == "MCP" and task.submission is not None:
            arguments = history.read_content(task.submission)["arguments"]
            out[arguments["record_id"]] = task
    return out


async def _close(app: Any, external: ExternalTasks, release: asyncio.Event) -> None:
    release.set()
    await external.close()
    await app.state.task_recovery.drain()


@pytest.mark.asyncio
@pytest.mark.timeout(90)  # in-memory MCP sessions plus a blocked writer
async def test_a_read_only_reply_returns_before_its_record_and_later_reads_see_it(
    arc: ARCMemory, tmp_path: Path
) -> None:
    """The writer is busy: the read-only call still answers; its record follows, and a
    later operation on the writer already sees it (FIFO), with no sleep.

    SABOTAGE: await ``_record_or_defer`` for every inline outcome in ``_call`` (the old
    path) and the call cannot answer while the writer is busy.
    """
    app, external, calls, release = await _setup(arc, tmp_path)
    history = app.state.task_history
    gate = threading.Event()
    try:
        await calls("read", "R0")  # opens the connection
        await external.outcomes.recorded()
        history.io.submit(gate.wait, 30)  # the history writer is busy
        reply = await asyncio.wait_for(calls("read", "R1"), 15)
        assert not reply.is_error and reply.structured_content == {"n": "1"}
        assert external.outcomes.pending == 1
        assert set(await asyncio.to_thread(_mcp_tasks, app, calls.root.owner.session_id)) == {"R0"}
        gate.set()
        later = await history.io.run(
            _mcp_tasks, app, calls.root.owner.session_id
        )  # queued after the record
        assert set(later) == {"R0", "R1"}
        settled = later["R1"]
        assert settled.status == "completed" and settled.raw_status == "inline"
        result = await asyncio.to_thread(history.read_content, settled.result)
        assert result["structuredContent"] == {"n": "1"}
        await external.outcomes.recorded()
        assert external.outcomes.pending == 0
    finally:
        gate.set()
        await _close(app, external, release)


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_group_committed_records_equal_records_written_one_at_a_time(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Four records written one at a time and four queued behind a busy writer (one
    group) leave the same lane and ``_events`` records, in queue order per session; the
    group takes one ``put_many`` per rank and per index kind, and no per-record put.

    SABOTAGE: make ``SegmentGroup.stage`` return False (no staging) and the group puts
    each record on its own.
    """
    app, external, calls, release = await _setup(arc, tmp_path)
    history = app.state.task_history
    sid = calls.root.owner.session_id
    order: list[str] = []
    record = external.outcomes.record

    def noted(identity: str, write: Any, **kwargs: Any) -> None:
        order.append(identity.removeprefix("external-inline:"))
        record(identity, write, **kwargs)

    monkeypatch.setattr(external.outcomes, "record", noted)
    gate = threading.Event()
    try:
        grouped = history.run_grouped

        def one_at_a_time(operations: list[Any]) -> list[tuple[bool, Any]]:
            out: list[tuple[bool, Any]] = []
            for operation in operations:
                try:
                    out.append((True, operation.context.run(operation.write)))
                except Exception as error:  # noqa: BLE001 - reported per operation
                    out.append((False, error))
            return out

        monkeypatch.setattr(history, "run_grouped", one_at_a_time)
        for index in range(4):
            await calls("read", f"S{index}")
            await external.outcomes.recorded()
        monkeypatch.setattr(history, "run_grouped", grouped)

        store = arc._store
        puts: list[tuple[str, str]] = []
        for method in ("put", "put_at", "put_many"):
            original = getattr(store, method)

            def counted(
                kind: str, *args: Any, _m: str = method, _f: Any = original, **kw: Any
            ) -> Any:
                puts.append((_m, kind))
                return _f(kind, *args, **kw)

            monkeypatch.setattr(store, method, counted)
        history.io.submit(gate.wait, 30)
        await asyncio.gather(*(calls("read", f"G{index}") for index in range(4)))
        assert external.outcomes.pending == 4
        gate.set()
        await external.outcomes.recorded()
        assert sorted(puts) == [
            ("put_many", "segments"),  # task content
            ("put_many", "segments"),  # the task-event lane
            ("put_many", "segments"),  # the _events log
            ("put_many", "task_lookup"),
        ]
    finally:
        gate.set()
        await _close(app, external, release)

    def recorded() -> tuple[list[Any], list[Any], dict[str, Any], dict[str, str], dict[str, Any]]:
        arc._segments.release(sid)  # read clio-core's records, not the cache
        lane = [content for _sid, content in session_task_events(arc, sid)]
        events = [
            segment.content
            for segment in arc._live.iter_session_event_segments(sid)
            if isinstance(segment.content, dict)
            and segment.content.get("event_type") in {"task.submission", "task.observed"}
        ]
        tasks = _mcp_tasks(app, sid)
        references = {
            str(history.read_content(task.submission)["reference"]): key
            for key, task in tasks.items()
        }
        results = {key: history.read_content(task.result) for key, task in tasks.items()}
        return lane, events, tasks, references, results

    lane, events, tasks, reference, results = await asyncio.to_thread(recorded)
    by_handle = {task.handle: key for key, task in tasks.items()}

    def call_of(content: dict[str, Any]) -> str | None:
        rec = content["payload"]["record"]
        return by_handle.get(rec.get("handle")) or reference.get(rec.get("reference"))

    def shape(content: dict[str, Any]) -> tuple[Any, ...]:
        rec = content["payload"]["record"]
        if content["event_type"] == "task.submission":
            return ("task.submission",)
        return ("task.observed", rec["status"], rec["raw_status"], rec["settlement"], rec["tool"])

    ours = [(call_of(content), shape(content)) for content in lane if call_of(content)]
    sequence = [key for key, _shape in ours]
    # Each call: its submission, then its settled snapshot; written in queue order.
    expected = [key for key in ["S0", "S1", "S2", "S3"] for _ in range(2)]
    grouped_keys = [by_handle[handle] for handle in order[4:]]
    expected += [key for key in grouped_keys for _ in range(2)]
    assert sequence == expected
    shapes = {key: [s for k, s in ours if k == key] for key in by_handle.values()}
    for index in range(4):
        assert shapes[f"G{index}"] == shapes[f"S{index}"]
        assert shapes[f"G{index}"][0] == ("task.submission",)
        assert shapes[f"G{index}"][1] == (
            "task.observed",
            "completed",
            "inline",
            "external_acknowledged",
            "read",
        )
    # The canonical log holds the same task events in the same order.
    logged = [(call_of(content), shape(content)) for content in events if call_of(content)]
    assert logged == ours
    for index in range(4):
        assert results[f"G{index}"] == results[f"S{index}"]
        assert results[f"G{index}"]["structuredContent"] == {"n": str(index)}


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_failed_record_is_typed_and_recovered(
    arc: ARCMemory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """clio-core loses the group's puts: the reply is unaffected, the record is logged
    ``read_only_outcome_deferred`` and handed to recovery, which records it once the
    writer is reconciled.

    SABOTAGE: drop ``self.recovery.defer`` from ``ReadOnlyOutcomes._finished`` and the
    record is never pending, nor written.
    """
    app, external, calls, release = await _setup(arc, tmp_path)
    history = app.state.task_history
    store = arc._store
    original = store.put_many
    losing = {"on": True}

    def put_many(kind: str, records: Any) -> None:
        if losing["on"]:
            raise BatchPutError(kind, [], {r.name: RuntimeError("lost") for r in records})
        original(kind, records)

    try:
        await calls("read", "R0")
        await external.outcomes.recorded()
        monkeypatch.setattr(store, "put_many", put_many)
        with caplog.at_level(logging.ERROR, logger="clio_agent.gact.read_only_outcomes"):
            reply = await calls("read", "F1")
            await external.outcomes.recorded()
        assert not reply.is_error and reply.structured_content == {"n": "1"}
        assert f"reason={READ_ONLY_OUTCOME_DEFERRED}" in caplog.text
        pending = [p for p in app.state.task_recovery.pending if p.startswith("external-inline:")]
        assert len(pending) == 1 and history.requires_recovery
        assert "F1" not in await asyncio.to_thread(_mcp_tasks, app, calls.root.owner.session_id)
        losing["on"] = False
        await app.state.task_recovery.drain()
        assert not history.requires_recovery
        assert not app.state.task_recovery.is_pending(pending[0])
        settled = (await asyncio.to_thread(_mcp_tasks, app, calls.root.owner.session_id))["F1"]
        assert settled.status == "completed" and settled.raw_status == "inline"
    finally:
        losing["on"] = False
        await _close(app, external, release)


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_crash_before_the_record_leaves_nothing_to_recover_unlike_a_write(
    arc: ARCMemory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash before a read-only call's record lands leaves no task, so recovery has
    nothing to resubmit or report; a write call in flight was recorded before it was
    sent and recovery reports it unknown (write-ahead unchanged).

    SABOTAGE: make ``skips_write_ahead`` return False for the read tool and the read
    call is recorded before it is sent (its task exists after the "crash").
    """
    app, external, calls, release = await _setup(arc, tmp_path)
    lost: list[str] = []
    monkeypatch.setattr(
        external.outcomes, "record", lambda identity, _write, **_kwargs: lost.append(identity)
    )
    writing: asyncio.Task[Any] | None = None
    try:
        reply = await calls("read", "C1")  # answered; its record never reaches the writer
        assert not reply.is_error
        writing = asyncio.create_task(calls("write", "W1"))
        await asyncio.wait_for(calls.started.wait(), 15)  # type: ignore[attr-defined]
        await external.restore()  # what boot recovery does with clio-core's records
        tasks = await asyncio.to_thread(_mcp_tasks, app, calls.root.owner.session_id)
        assert "C1" not in tasks and len(lost) == 1
        assert not any(
            lost[0].removeprefix("external-inline:") in p for p in app.state.task_recovery.pending
        )
        unknown = tasks["W1"]
        assert unknown.status == "task_state_uncertain"
        assert "Acceptance identity is unknown" in unknown.progress.message
    finally:
        release.set()
        if writing is not None:
            await asyncio.gather(writing, return_exceptions=True)
        await _close(app, external, release)
