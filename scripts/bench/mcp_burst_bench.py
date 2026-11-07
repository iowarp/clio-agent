"""Read-only MCP call burst through ``ExternalTasks`` against the running clio-core daemon.

``uv run --frozen --no-sync python -m scripts.bench.mcp_burst_bench [--calls N]
[--concurrency C] [--sessions K] [--namespace bench-...] [--json PATH]``

With ``--sessions K`` the calls are spread over K sessions that share the one history
writer and the server's connection, each with its own in-flight bound: the shape of several agents
working at once. The report adds each session's p90 latency and the process's CPU over
wall time (near 1.0: the interpreter lock is the limit; well under it with latency
rising: waiting on clio-core).

Reproduces the P7 shape (a script gathering many ``get_record`` calls to a local MCP
server whose own handler takes milliseconds) without a model or a turn: ``--calls``
read-only calls go through :meth:`ExternalTasks.call` with at most ``--concurrency`` in
flight (``tools.per_server_concurrency``), against an in-process FastMCP server. It
reports:

- **calls/s** and the per-call latency the caller sees (until ``call`` returns);
- **settle_ms**: the time after the last reply until every outcome record is in
  clio-core (the history writer's backlog; zero when the caller waits for its record);
- **store calls and puts per call**, by method;
- **writer ms per call**: time the history writer (``CoreIO``) spent per call, the
  serialized work every call shares.

It writes only under its own ARC namespace (default ``bench-burst``), deletes every
record it wrote and its temporary folder, and leaves the daemon running (it deregisters
its client, so the last-client-out daemon stop never runs from here).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from scripts.bench.arc_bench import _cleanup, _Counting


class _TimedIO:
    """Sums the time the history writer (``CoreIO``) spends running operations.

    Wraps the one queueing path every operation takes (``CoreIO._submit``; before group
    commit, ``CoreIO.submit``), so coalesced batches are timed too.
    """

    def __init__(self, io: Any) -> None:
        self.busy = 0.0
        self.jobs = 0
        name = "_submit" if hasattr(io, "_submit") else "submit"
        original = getattr(io, name)

        def submit(operation: Any, /, *args: Any, **kwargs: Any) -> Any:
            def timed() -> Any:
                start = time.perf_counter()
                try:
                    return operation(*args, **kwargs)
                finally:
                    self.busy += time.perf_counter() - start
                    self.jobs += 1

            return original(timed)

        setattr(io, name, submit)


def _environment(arc: Any, work: Path, n_sessions: int = 1) -> tuple[Any, list[str]]:
    """Sessions with an active root assignment each and a workspace (store work, off-loop).

    They share one task history, so one history writer, as every session of a server does.
    """
    from clio_agent.arc.task_types import TaskOwner, TaskSnapshot
    from clio_agent.gact.native_work import now
    from clio_agent.gact.sessions import SessionStore
    from clio_agent.gact.task_history import TaskHistory
    from clio_agent.gact.workspaces import WorkspaceStore

    sessions = SessionStore(path=work / "sessions.json", store=arc._store)
    workspaces = WorkspaceStore(path=work / "workspaces.json")
    workspace = workspaces.create(name="bench", root_path=str(work))
    history, stamp = TaskHistory(arc), now()
    history.index_build_pending = False
    sids = []
    for _ in range(n_sessions):
        session = sessions.create(workspace_id=workspace.id, agent={"id": "main"})
        sids.append(session.id)
        history.record_snapshot(
            TaskSnapshot(
                handle="task_root_" + uuid.uuid4().hex[:8],
                kind="Session",
                tool="conversation",
                owner=TaskOwner(human_id="bench", session_id=session.id, assignment_id="root"),
                label="Bench",
                description="Burst bench assignment",
                description_missing=False,
                invocation_id=f"bench-assignment-{session.id}",
                created_at=stamp,
                updated_at=stamp,
                status="active",
            ),
            observation_id=uuid.uuid4().hex,
        )
    state = SimpleNamespace(
        sessions=sessions,
        workspaces=workspaces,
        task_history=history,
        human_principal="bench",
        permission_policies=[],
        # Admission control is the conversation engine's; the bench measures the writer.
        conversations=SimpleNamespace(require_admission=lambda _task: None),
    )
    return SimpleNamespace(state=state), sids


def _server(handler_ms: float) -> Any:
    from fastmcp import FastMCP
    from mcp.types import ToolAnnotations

    server = FastMCP("records")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def get_record(record_id: str) -> dict[str, str]:
        await asyncio.sleep(handler_ms / 1000)
        return {"record_id": record_id, "value": "v" * 200}

    return server


async def _burst(
    arc: Any,
    work: Path,
    counter: _Counting,
    calls: int,
    concurrency: int,
    handler_ms: float,
    n_sessions: int = 1,
) -> dict[str, Any]:
    from fastmcp import Client

    from clio_agent.gact import context
    from clio_agent.gact.core_recovery import CoreRecovery
    from clio_agent.gact.external_tasks import ExternalTasks
    from clio_agent.tools.task_call_context import TASK_CALL, TaskCall

    app, sids = await asyncio.to_thread(_environment, arc, work, n_sessions)
    history = app.state.task_history
    app.state.task_recovery = CoreRecovery(history)
    timed = _TimedIO(history.io)
    external = ExternalTasks(app)
    # One connection to the server (sessions in a workspace share its MCP servers, and
    # the call path keeps one connection per server name); each session has its own
    # in-flight bound, as on a server.
    shared = Client(_server(handler_ms))
    clients = [shared for _ in sids]
    gates = [asyncio.Semaphore(concurrency) for _ in sids]
    latencies: list[float] = []
    per_session: dict[str, list[float]] = {sid: [] for sid in sids}

    async def one(index: int) -> None:
        slot = index % len(sids)
        sid, client = sids[slot], clients[slot]
        async with gates[slot]:
            tokens = (context.set_app(app), context.set_session_id(sid))
            call = TASK_CALL.set(TaskCall(invocation_id=f"inv-{index}"))
            start = time.perf_counter()
            try:
                reply = await external.call(
                    "records", client, "get_record", {"record_id": f"R{index}"}, auth_scope="bench"
                )
            finally:
                TASK_CALL.reset(call)
                for token in reversed(tokens):
                    context.reset(token)
            latencies.append((time.perf_counter() - start) * 1e3)
            per_session[sid].append(latencies[-1])
            if reply.is_error:
                raise RuntimeError(f"call {index} failed: {reply}")

    for warm in range(len(sids)):  # open each connection and warm each session's chunks
        await one(-(warm + 1))  # negative indices: one per slot, never a measured id
    await history.io.run(lambda: None)
    counter.reset()
    timed.busy, timed.jobs = 0.0, 0
    latencies.clear()
    for rows in per_session.values():
        rows.clear()
    cpu_start = time.process_time()
    start = time.perf_counter()
    await asyncio.gather(*(one(index) for index in range(calls)))
    replied = time.perf_counter() - start
    await history.io.run(lambda: None)  # the writer is FIFO: every queued record is in
    settled = time.perf_counter() - start
    cpu = time.process_time() - cpu_start
    store_calls = dict(counter.calls)
    busy = timed.busy
    await external.close()
    await app.state.task_recovery.drain()

    def recorded() -> int:
        return sum(
            1
            for sid in sids
            for handle in history.task_handles(session_id=sid)
            if (task := history.task_snapshot(handle)) is not None
            and task.kind == "MCP"
            and task.terminal_at is not None
        )

    settled_tasks = await asyncio.to_thread(recorded)
    puts = sum(n for (method, _kind), n in store_calls.items() if method.startswith("put"))
    by_method: dict[str, int] = {}
    for (method, kind), n in sorted(store_calls.items()):
        by_method[f"{method}:{kind}"] = n
    await history.io.close()
    return {
        "calls": calls,
        "concurrency": concurrency,
        "handler_ms": handler_ms,
        "calls_per_s": round(calls / replied, 1),
        "reply_wall_s": round(replied, 3),
        "settle_ms": round((settled - replied) * 1e3, 1),
        "latency_p50_ms": round(statistics.median(latencies), 2),
        "latency_p90_ms": round(sorted(latencies)[int(len(latencies) * 0.9) - 1], 2),
        "store_calls_per_call": round(sum(store_calls.values()) / calls, 2),
        "puts_per_call": round(puts / calls, 2),
        "writer_ms_per_call": round(busy * 1e3 / calls, 2),
        "writer_jobs_per_call": round(timed.jobs / calls, 2),
        "store_calls": by_method,
        "terminal_tasks_recorded": settled_tasks - len(sids),  # less the warm-up calls
        "sessions": len(sids),
        # Process CPU over wall time: near 1.0 means one core busy (the interpreter lock);
        # well under it with high latency means waiting on clio-core.
        "process_cpu_per_wall": round(cpu / settled, 2),
        "session_p90_ms": sorted(
            round(sorted(rows)[max(int(len(rows) * 0.9) - 1, 0)], 1)
            for rows in per_session.values()
            if rows
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mcp_burst_bench", description=__doc__.split("\n")[0])
    parser.add_argument("--namespace", default="bench-burst")
    parser.add_argument("--calls", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--handler-ms", type=float, default=2.0)
    parser.add_argument(
        "--sessions", type=int, default=1, help="sessions sharing the one history writer"
    )
    parser.add_argument("--json", default="", help="also write the results to this file")
    args = parser.parse_args(argv)
    if not args.namespace.startswith("bench"):
        parser.error("the namespace must start with 'bench' so no real data is touched")

    from clio_agent.arc import storage
    from clio_agent.arc.memory import ARCMemory

    store = storage.make_arc_store(namespace=args.namespace)
    work = Path(tempfile.mkdtemp(prefix="mcp-burst-bench-"))
    counter = _Counting(store)
    results: dict[str, Any] = {"namespace": args.namespace}
    code = 0
    try:
        arc = ARCMemory(data_dir=str(work), store=store)
        results["burst"] = asyncio.run(
            _burst(arc, work, counter, args.calls, args.concurrency, args.handler_ms, args.sessions)
        )
    except Exception as exc:  # noqa: BLE001 - report, then still clean up
        results["error"] = f"{type(exc).__name__}: {exc}"
        code = 1
    finally:
        counter.restore()
        results["cleanup_deleted"] = _cleanup(store)
        shutil.rmtree(work, ignore_errors=True)
        text = json.dumps(results, indent=2)
        print(text)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
        sys.stdout.flush()
        storage._deregister_client()  # never stop the shared daemon from here
        os._exit(code)


if __name__ == "__main__":
    sys.exit(main())
