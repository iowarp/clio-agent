"""ARC write-path benchmark against the running clio-core daemon.

``uv run --frozen --no-sync python -m scripts.bench.arc_bench [--namespace N] [--json PATH]``

Measures what the ARC performance review (``docs/design/arc-performance-review-2026-11.md``)
found slow, so each change can be measured before and after:

- **primitives:** a guarded small put, a raw put without the guard, a get, and puts of
  growing size;
- **append:** latency of one event append as the active chunk fills, and bytes sent;
- **call:** the task-history writes of one inline MCP call (submission, accepted,
  result), with the store calls they make;
- **burst:** eight such calls submitted at once through ``CoreIO``, as concurrent tool
  calls are.

It writes only under its own ARC namespace (default ``bench-arc``), deletes every record
it wrote, and leaves the daemon running: it deregisters its client before exiting, so
the last-client-out daemon stop never runs from here. Run it while no evaluation is
running, since it shares the daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from typing import Any


def _stats(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50_ms": round(statistics.median(ordered), 3),
        "p90_ms": round(ordered[max(0, int(len(ordered) * 0.9) - 1)], 3),
        "n": len(ordered),
    }


def _time(fn: Callable[[], Any], n: int) -> list[float]:
    out = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        out.append((time.perf_counter() - start) * 1e3)
    return out


class _Counting:
    """Counts store calls and the bytes ``put`` sends, by method and kind."""

    METHODS = ("put", "put_many", "get", "delete", "exists")

    def __init__(self, store: Any) -> None:
        self.store = store
        self.calls: collections.Counter[tuple[str, str]] = collections.Counter()
        self.bytes = 0
        self._orig = {m: getattr(store, m) for m in self.METHODS if hasattr(store, m)}
        for name, fn in self._orig.items():
            setattr(store, name, self._wrap(name, fn))

    def _wrap(self, name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(kind: str, *args: Any, **kwargs: Any) -> Any:
            self.calls[(name, kind)] += 1
            if name == "put" and len(args) >= 2:
                self.bytes += len(args[1])
            return fn(kind, *args, **kwargs)

        return wrapped

    def reset(self) -> None:
        self.calls.clear()
        self.bytes = 0

    def restore(self) -> None:
        for name, fn in self._orig.items():
            setattr(self.store, name, fn)


def primitives(store: Any) -> dict[str, Any]:
    from clio_agent.arc.blob_frame import stored_body
    from clio_agent.arc.clio_core_async_ops import await_future

    body = stored_body(os.urandom(2000))
    tag_id = store._tag_ids.get("task_lookup")

    def raw_put() -> None:
        future = await_future(
            store._client.AsyncPutBlob(tag_id, "bench-raw", body, 0), op_name="put"
        )
        assert future.wait(0) == 0

    out = {
        "raw_put_2kb_unguarded": _stats(_time(raw_put, 200)),
        "put_300b_guarded": _stats(
            _time(lambda: store.put("task_lookup", "bench-small", b"x" * 300), 200)
        ),
        "get_hit_300b": _stats(_time(lambda: store.get("task_lookup", "bench-small"), 200)),
    }
    for size in (20_000, 400_000, 1_500_000):
        data = os.urandom(size)
        out[f"put_{size}b"] = _stats(
            _time(lambda data=data: store.put("segments", "bench~events~big", data), 20)
        )
    return out


def appends(arc: Any, counter: _Counting) -> dict[str, Any]:
    from clio_agent.arc import lane_chunking

    segments = arc._segments
    payload = {"event_type": "task.observed", "payload": {"record": {"blob": "y" * 1700}}}
    sid = "sess_bench_" + uuid.uuid4().hex[:8]
    marks = {1, 64, 128, 256, 384, 511}
    latency: dict[str, float] = {}
    samples = []
    counter.reset()
    for index in range(1, 513):
        start = time.perf_counter()
        scope = lane_chunking.chunk_for_append(segments, sid, "_events", capacity=512)
        segments.append(sid, scope, "semantic_event", payload, step=-1)
        elapsed = (time.perf_counter() - start) * 1e3
        samples.append(elapsed)
        if index in marks:
            latency[f"fill_{index}_ms"] = round(elapsed, 3)
    return {
        "per_append": _stats(samples),
        "by_fill": latency,
        "bytes_per_append": round(counter.bytes / 512),
        "puts_per_append": round(sum(counter.calls.values()) / 512, 2),
    }


def _one_call(history: Any, sid: str) -> dict[str, float]:
    from clio_agent.arc.task_types import (
        BackendReference,
        SubmissionContext,
        TaskOwner,
        TaskSnapshot,
        argument_digest,
    )
    from clio_agent.gact.native_work import now

    handle, stamp = "task_" + uuid.uuid4().hex, now()
    owner = TaskOwner(
        human_id="bench", session_id=sid, assignment_id="task_assign", parent_handle=None
    )
    args = {"id": uuid.uuid4().hex}
    submission = SubmissionContext(
        reference=uuid.uuid4().hex,
        handle=handle,
        owner=owner,
        invocation_id=uuid.uuid4().hex,
        tool="get_record",
        argument_digest=argument_digest(args),
        arguments=args,
        output_schema=None,
        workspace="/tmp/bench-ws",
        permissions={"mode": "bench"},
        confinement={},
        attribution={"agent": "bench"},
        execution_limits={},
        backend=BackendReference(
            owner="external_mcp",
            server="bench",
            auth_scope="a" * 64,
            transport="StreamableHttpTransport",
        ),
    )
    timings = {}
    start = time.perf_counter()
    with history.serialized():
        content = history.record_submission(submission)
        history.record_snapshot(
            TaskSnapshot(
                handle=handle,
                kind="MCP",
                owner=owner,
                description="Call get_record",
                description_missing=False,
                label="get_record",
                tool="get_record",
                invocation_id=submission.invocation_id,
                created_at=stamp,
                updated_at=stamp,
                submission=content,
                supported_actions=("observe", "wait", "cancel"),
            ),
            observation_id=uuid.uuid4().hex,
        )
    timings["admission_ms"] = (time.perf_counter() - start) * 1e3
    start = time.perf_counter()
    with history.serialized():
        current = history.task_snapshot(handle)
        result = history.write_content(
            sid, {"content": [{"type": "text", "text": "ok"}], "isError": False}
        )
        history.record_snapshot(
            current.model_copy(
                update={
                    "status": "completed",
                    "raw_status": "inline",
                    "terminal_at": now(),
                    "updated_at": now(),
                    "settlement": "external_acknowledged",
                    "result": result,
                    "backend_observation": result,
                    "supported_actions": ("get_result",),
                }
            ),
            observation_id=uuid.uuid4().hex,
        )
    timings["result_ms"] = (time.perf_counter() - start) * 1e3
    return timings


def calls(arc: Any, counter: _Counting) -> dict[str, Any]:
    from clio_agent.gact.core_io import CoreIO
    from clio_agent.gact.task_history import TaskHistory

    history = TaskHistory(arc)
    history.index_build_pending = False
    sid = "sess_benchc_" + uuid.uuid4().hex[:8]
    _one_call(history, sid)  # warm the session and human rows
    counter.reset()
    runs = [_one_call(history, sid) for _ in range(10)]
    per_call = {
        "admission": _stats([r["admission_ms"] for r in runs]),
        "result": _stats([r["result_ms"] for r in runs]),
        "total": _stats([r["admission_ms"] + r["result_ms"] for r in runs]),
        "store_calls_per_call": round(sum(counter.calls.values()) / 10, 1),
        "bytes_per_call": round(counter.bytes / 10),
    }
    io = CoreIO()

    async def burst() -> list[float]:
        start = time.perf_counter()
        done: list[float] = []

        async def one() -> None:
            await io.run(_one_call, history, "sess_burst_" + uuid.uuid4().hex[:8])
            done.append((time.perf_counter() - start) * 1e3)

        await asyncio.gather(*(one() for _ in range(8)))
        await io.close()
        return sorted(done)

    finished = asyncio.run(burst())
    return {
        "per_call": per_call,
        "burst_8_finish_ms": [round(x) for x in finished],
        "burst_8_last_ms": round(finished[-1]),
    }


def _cleanup(store: Any) -> int:
    from clio_agent.arc.clio_core_async_ops import store_delete_many

    total = 0
    for kind in store._kinds:
        names = list(store._cte.Tag(store.tag(kind)).GetContainedBlobs())
        for start in range(0, len(names), 64):
            store_delete_many(store, kind, names[start : start + 64])
        total += len(names)
    return total


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="arc_bench", description=__doc__.split("\n")[0])
    parser.add_argument("--namespace", default="bench-arc")
    parser.add_argument("--json", default="", help="also write the results to this file")
    parser.add_argument("--only", default="", help="comma list of sections: primitives,append,call")
    args = parser.parse_args(argv)
    if not args.namespace.startswith("bench"):
        parser.error("the namespace must start with 'bench' so no real data is touched")

    from clio_agent.arc import storage
    from clio_agent.arc.memory import ARCMemory

    store = storage.make_arc_store(namespace=args.namespace)
    work = tempfile.mkdtemp(prefix="arc-bench-")
    counter = _Counting(store)
    sections = set(filter(None, args.only.split(","))) or {"primitives", "append", "call"}
    results: dict[str, Any] = {"namespace": args.namespace}
    code = 0
    try:
        arc = ARCMemory(data_dir=work, store=store)
        if "primitives" in sections:
            results["primitives"] = primitives(store)
        if "append" in sections:
            results["append"] = appends(arc, counter)
        if "call" in sections:
            results["call"] = calls(arc, counter)
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
