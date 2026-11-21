"""ARC store put/get latency and throughput through CLIO's own store path.

Phase 1, payload sweep: one client, put then get of N unique blobs per size.
Phase 2, concurrency: 1, 4 and 16 client PROCESSES (each attaches its own native
client to the one daemon), started together on a barrier.
The daemon's RSS is sampled throughout.

Usage (run once per iowarp-core venv)::

    <venv>/bin/python scripts/bench/arc_store_bench.py --home .bench/h250 \
        --out .bench/results/arc_store_2.5.0.json
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bench_common as bc  # noqa: E402

DEFAULT_SIZES = "1K,4K,16K,64K,256K,1M,4M,16M"


def _iters_for(size: int, budget_bytes: int, floor: int, ceiling: int) -> int:
    return max(floor, min(ceiling, budget_bytes // max(size, 1)))


def _client(
    home: str, client_id: int, size: int, iters: int, barrier: object, queue: object
) -> None:
    bc.use_home(Path(home))
    store = bc.open_store()
    payload = os.urandom(size)
    keys = [f"c{client_id}_{size}_{i}_{time.time_ns()}" for i in range(iters)]
    puts: list[float] = []
    gets: list[float] = []
    errors = 0
    barrier.wait()  # type: ignore[attr-defined]
    start = time.perf_counter()
    for key in keys:
        t = time.perf_counter()
        try:
            store.put("segments", key, payload)
        except Exception as exc:  # noqa: BLE001 - counted and reported
            errors += 1
            print(f"put error: {exc!r}"[:300], file=sys.stderr)
        puts.append(time.perf_counter() - t)
    put_wall = time.perf_counter() - start
    start = time.perf_counter()
    for key in keys:
        t = time.perf_counter()
        try:
            got = store.get("segments", key)
            if got is None or len(got) != size:
                errors += 1
        except Exception as exc:  # noqa: BLE001 - counted and reported
            errors += 1
            print(f"get error: {exc!r}"[:300], file=sys.stderr)
        gets.append(time.perf_counter() - t)
    get_wall = time.perf_counter() - start
    queue.put((puts, gets, put_wall, get_wall, errors))  # type: ignore[attr-defined]


def run_round(home: Path, clients: int, size: int, iters: int) -> dict[str, object]:
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(clients)
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_client, args=(str(home), i, size, iters, barrier, queue))
        for i in range(clients)
    ]
    for p in procs:
        p.start()
    results = [queue.get(timeout=1800) for _ in procs]
    for p in procs:
        p.join(timeout=120)
    puts = [x for r in results for x in r[0]]
    gets = [x for r in results for x in r[1]]
    put_wall = max(r[2] for r in results)
    get_wall = max(r[3] for r in results)
    total = size * iters * clients
    return {
        "clients": clients,
        "size_bytes": size,
        "iters_per_client": iters,
        "errors": sum(r[4] for r in results),
        "put": bc.percentiles(puts),
        "get": bc.percentiles(gets),
        "put_ops_s": round(len(puts) / put_wall, 1),
        "get_ops_s": round(len(gets) / get_wall, 1),
        "put_mib_s": round(total / put_wall / 2**20, 2),
        "get_mib_s": round(total / get_wall / 2**20, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--sizes", default=DEFAULT_SIZES)
    parser.add_argument("--clients", default="1,4,16")
    parser.add_argument("--concurrency-sizes", default="4K,1M")
    parser.add_argument("--budget", default="256M", help="bytes per size per client")
    args = parser.parse_args()

    bc.use_home(args.home)
    store = bc.open_store()  # spawns (or adopts) this home's daemon
    pid = bc.daemon_pid()
    budget = bc.parse_size(args.budget)
    sweep, concurrency = [], []
    with bc.RssSampler(pid, interval=1.0) as rss:
        for text in args.sizes.split(","):
            size = bc.parse_size(text)
            iters = _iters_for(size, budget, floor=16, ceiling=2000)
            row = run_round(args.home, 1, size, iters)
            sweep.append(row)
            print(f"sweep {text}: {row}", file=sys.stderr)
        for text in args.concurrency_sizes.split(","):
            size = bc.parse_size(text)
            for clients in (int(c) for c in args.clients.split(",")):
                iters = _iters_for(size, budget // max(clients, 1), floor=8, ceiling=500)
                row = run_round(args.home, clients, size, iters)
                concurrency.append(row)
                print(f"concurrency {text} x{clients}: {row}", file=sys.stderr)
    bc.write_json(
        args.out,
        {
            "iowarp_core": bc.core_version(),
            "daemon_pid": pid,
            "sweep": sweep,
            "concurrency": concurrency,
            "daemon_rss": rss.summary(),
            "daemon_rss_series": rss.samples[::5],
        },
    )
    print(bc.stop_daemon(store), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
