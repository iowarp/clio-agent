"""Transcript write cost with and without the recall index's write observer.

``uv run --frozen --no-sync python -m scripts.bench.recall_index_bench [--appends N]``

Appends ``N`` (default 200) message-part atoms to one conversation's transcript lane
(``part_atoms.append_part_atom``, the path every transcript write takes) on a fresh ARC
three times: without the recall index, and with it attached (its rebuild delay is long,
so this measures the write path alone). It prints the per-append latency and the store
writes by record kind. The index adds no put per write: one durable marker put for the
conversation's first write, then nothing.

Without ``--namespace`` it runs on an in-process store; with one (it must start with
``bench``) it runs on the clio-core daemon the environment points at, writes only that
namespace, deletes what it wrote and deregisters without stopping the daemon. Prefer a
private daemon (``CLIO_RUNTIME_STATE_DIR`` and a private config) over a shared one.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from typing import Any


class _Counting:
    """Counts a store's writes by kind (``put_many`` counts each record once)."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self.writes: dict[str, int] = {}
        self._inside = False
        self._saved = {name: getattr(store, name) for name in ("put", "put_at", "put_many")}
        store.put = self._wrap("put")
        store.put_at = self._wrap("put_at")
        store.put_many = self._wrap("put_many")

    def _wrap(self, name: str) -> Any:
        original = self._saved[name]

        def call(kind: str, *args: Any, **kwargs: Any) -> Any:
            records = list(args[0]) if name == "put_many" else None
            if not self._inside:  # a store's put_many may put each record itself
                self.writes[kind] = self.writes.get(kind, 0) + (len(records) if records else 1)
            if records is None:
                return original(kind, *args, **kwargs)
            self._inside = True
            try:
                return original(kind, records, *args[1:], **kwargs)
            finally:
                self._inside = False

        return call

    def restore(self) -> None:
        for name, original in self._saved.items():
            setattr(self.store, name, original)


def _atom(n: int) -> dict[str, Any]:
    text = f"step {n}: the survey of the shelf continues with reading {n * 7}"
    return {
        "message_id": f"msg_{n}",
        "part_index": 0,
        "part": {"id": f"part_{n}", "type": "text", "text": text},
    }


def run(store: Any, appends: int, work: str) -> dict[str, Any]:
    """Measure ``appends`` transcript appends without and with the index (one store)."""

    from clio_agent.arc.memory import ARCMemory
    from clio_agent.gact.part_atoms import append_part_atom
    from clio_agent.gact.recall_index import RecallIndex
    from clio_agent.gact.transcript_projection import assemble_session_messages

    results: dict[str, Any] = {}
    for mode in ("without_index", "with_index", "without_index_again"):
        arc = ARCMemory(data_dir=os.path.join(work, mode), store=store)
        index = None
        if mode == "with_index":
            index = RecallIndex(
                store, lambda sid, arc=arc: assemble_session_messages(arc, sid), delay_s=3600.0
            )
            arc._segments.set_write_observer(index)
        counter = _Counting(store)
        sid = f"bench_{mode}_{os.getpid()}"
        samples: list[float] = []
        try:
            for n in range(appends):
                start = time.perf_counter()
                append_part_atom(arc._segments, sid, _atom(n))
                samples.append((time.perf_counter() - start) * 1e6)
        finally:
            counter.restore()
            if index is not None:
                index.close(1.0)
        ordered = sorted(samples)
        results[mode] = {
            "appends": appends,
            "mean_us": round(statistics.fmean(ordered), 1),
            "p50_us": round(statistics.median(ordered), 1),
            "p90_us": round(ordered[int(len(ordered) * 0.9) - 1], 1),
            "first_us": round(samples[0], 1),
            "writes_by_kind": dict(sorted(counter.writes.items())),
            "marker_puts": index.marker_puts if index is not None else 0,
        }
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recall_index_bench", description=__doc__.split("\n")[0])
    parser.add_argument("--appends", type=int, default=200)
    parser.add_argument("--namespace", default="", help="clio-core namespace (bench*)")
    args = parser.parse_args(argv)
    if args.namespace and not args.namespace.startswith("bench"):
        parser.error("the namespace must start with 'bench' so no real data is touched")

    from clio_agent.arc import storage
    from clio_agent.arc.live import _MemoryStore

    store = storage.make_arc_store(namespace=args.namespace) if args.namespace else _MemoryStore()
    work = tempfile.mkdtemp(prefix="recall-bench-")
    try:
        print(json.dumps(run(store, args.appends, work), indent=2))
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if args.namespace:
            store.clear()
            storage._deregister_client()  # never stop the shared daemon from here
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
