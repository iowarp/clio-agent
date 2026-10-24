"""Qualify clio-core's BM25 indexer for long-term memory (memory ledger P0 and M1).

Runs a private daemon (``tests/_cte_isolation.py``) with or without the
``clio_cte_indexer`` chimod scoped to memory documents and segment search companions,
then measures a first put, BM25 search, a clean restart, a killed daemon, and ARC's hot
path. Each phase is its own process, so a phase that starts after the previous one
exited is a clean daemon restart. State lives in ``.cloud-run/m1/``.

usage::

    probe=scripts/live_verification/memory_indexer_probe.py
    uv run --no-sync python $probe setup 1        # 1: with the indexer, 0: bare core
    uv run --no-sync python $probe put .cloud-run/m1/idx
    uv run --no-sync python $probe search .cloud-run/m1/idx
    uv run --no-sync python $probe hold .cloud-run/m1/idx &   # then: $probe kill <dir>
    uv run --no-sync python $probe bench .cloud-run/m1/idx
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / ".cloud-run" / "m1"
sys.path.insert(0, str(REPO))

INDEXER = """  - mod_name: clio_cte_indexer
    pool_name: clio_cte_indexer
    pool_query: local
    pool_id: "564.0"
    restart: true
    next_pool_id: "512.0"
    tag_re: "(memory/memories|segments)"
    blob_re: ".*\\\\.text"
    index_log_path: "{index_log}"
"""

DOCS = {
    "user/environments/frontier-allocation": "frontier allocation CSC123 batch walltime",
    "user/preferences/main": "prefers concise answers with citations",
    "project/ws_7f2/areas/hdf5-paper": "hdf5 chunking paper draft deadline november",
}


def setup(mode: str) -> None:
    """Write a private config (with the indexer when ``mode`` is ``1``) and its env."""
    from tests import _cte_isolation as iso  # noqa: PLC0415

    target = ROOT / ("idx" if mode == "1" else "bare")
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    env: dict[str, str] = {}
    info = iso.isolate_cte_env(target, env)
    if mode == "1":
        log = (target / "store" / "indexer_index.log").as_posix()
        info.config_path.write_text(info.config_path.read_text() + INDEXER.format(index_log=log))
        env["CLIO_CTE_POOL"] = "564.0"
    env["CLIO_ARC_STORE"] = "cte"
    (target / "env.json").write_text(json.dumps(env, indent=1))
    print(target)


def _load(target: Path) -> None:
    os.environ.update(json.loads((target / "env.json").read_text()))


def _store(namespace: str):  # noqa: ANN202 - returns the ARCStore under test
    from clio_agent.arc.storage import make_arc_store  # noqa: PLC0415

    return make_arc_store(namespace=namespace)


def _hits(store, kind: str, query: str) -> list[tuple[str, float]]:  # noqa: ANN001
    return [(name, round(score, 3)) for name, score in store.search(kind, query, k=8) if score]


def put(target: Path) -> None:
    """Attach, write three memory documents and two segment records, then search."""
    _load(target)
    started = time.perf_counter()
    memory = _store("memory")
    attached = time.perf_counter()
    first = None
    for name, text in DOCS.items():
        began = time.perf_counter()
        memory.put("memories", name, f"doc:{text}".encode(), search_text=text)
        first = first if first is not None else time.perf_counter() - began
    arc = _store("")
    arc.put("segments", "sess_x__/_events/m/0001", b"eventchunk frontier")
    arc.put("segments", "sess_x__/_search~root/0001", b"seg", search_text="frontier compacted")
    print(
        json.dumps(
            {
                "attach_s": round(attached - started, 3),
                "first_put_s": round(first or 0.0, 4),
                "supports_search": memory.supports_search(),
                "frontier": _hits(memory, "memories", "frontier"),
            }
        )
    )


def search(target: Path) -> None:
    """Search what earlier phases wrote; ``eventchunk`` lives only in an ``_events`` body."""
    _load(target)
    memory = _store("memory")
    began = time.perf_counter()
    out: dict[str, object] = {
        query: _hits(memory, "memories", query)
        for query in ("frontier", "citations", "hdf5", "quasar", "eventchunk")
    }
    out["segments:frontier"] = _hits(_store(""), "segments", "frontier")
    out["segments:eventchunk"] = _hits(_store(""), "segments", "eventchunk")
    out["search_s"] = round(time.perf_counter() - began, 4)
    print(json.dumps(out))


def hold(target: Path) -> None:
    """Write one more document, then stay attached so ``kill`` can SIGKILL the daemon."""
    _load(target)
    memory = _store("memory")
    memory.put("memories", "user/topics/quasar", b"doc:quasar", search_text="quasar telescope")
    print(json.dumps({"quasar": _hits(memory, "memories", "quasar")}), flush=True)
    time.sleep(120)


def kill(target: Path) -> None:
    """SIGKILL the private daemon named by its pidfile."""
    _load(target)
    from clio_agent.arc.storage import _daemon_pidfile  # noqa: PLC0415

    pid = int(_daemon_pidfile().read_text().split()[0])
    os.kill(pid, signal.SIGKILL)
    print(json.dumps({"killed": pid}))


def bench(target: Path) -> None:
    """ARC's hot path: ``_events`` puts (no companion) and segment puts (with one)."""
    _load(target)
    arc = _store("")
    payload = os.urandom(3000)
    events: list[float] = []
    segments: list[float] = []
    for i in range(400):
        began = time.perf_counter()
        arc.put("segments", f"sess_b__/_events/m/{i:05d}", payload)
        events.append(time.perf_counter() - began)
    for i in range(400):
        began = time.perf_counter()
        text = f"render {i} hdf5 frontier tokens " * 20
        arc.put("segments", f"sess_b__/_search~root/{i:05d}", payload, search_text=text)
        segments.append(time.perf_counter() - began)

    def summary(xs: list[float]) -> dict[str, float]:
        return {
            "p50_ms": round(statistics.median(xs) * 1e3, 3),
            "p95_ms": round(sorted(xs)[int(len(xs) * 0.95)] * 1e3, 3),
        }

    print(json.dumps({"events_put": summary(events), "segment_put": summary(segments)}))


if __name__ == "__main__":
    command, argument = sys.argv[1], sys.argv[2]
    if command == "setup":
        setup(argument)
    else:
        {"put": put, "search": search, "hold": hold, "kill": kill, "bench": bench}[command](
            Path(argument)
        )
