"""Sustained-load soak of one clio-core daemon (reproduces iowarp/clio-agent#1846).

#1846: late in long runs a store call passes CLIO's 15 s bound and the daemon's
memory reaches 2.7 GiB. This drives one daemon with ``--clients`` processes for
``--minutes`` with the mix that issue names: many small appends (the A2UI load
test's 170k appends), growing transcript blobs rewritten in place (session
segments), and reads of both. Every op's latency is kept per minute; the daemon's
RSS is sampled every ``--rss-interval`` seconds.

Reported per minute: ops, p50/p99/max latency, ops over 1 s and over 15 s, errors,
and the daemon RSS. Usage::

    <venv>/bin/python scripts/bench/soak.py --home .bench/soak250 --minutes 30 \
        --out .bench/results/soak_2.5.0.json
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bench_common as bc  # noqa: E402

_SLOW_S = 1.0
_BOUND_S = 15.0
_TRANSCRIPT_CAP = 2 * 1024 * 1024


def _client(home: str, client_id: int, deadline: float, queue: object) -> None:
    bc.use_home(Path(home))
    store = bc.open_store()
    rng = random.Random(client_id)
    minute: dict[int, dict[str, object]] = {}
    start = time.monotonic()
    small_keys: list[str] = []
    transcript = b""
    session = 0
    appended = 0
    while time.time() < deadline:
        roll = rng.random()
        t = time.perf_counter()
        op = "small_put"
        try:
            if roll < 0.6:
                key = f"a2ui_{client_id}_{appended}"
                store.put("segments", key, os.urandom(rng.randint(256, 8192)))
                small_keys.append(key)
                appended += 1
            elif roll < 0.8:
                op = "transcript_put"
                transcript += os.urandom(rng.randint(2048, 65536))
                if len(transcript) > _TRANSCRIPT_CAP:
                    session += 1
                    transcript = b""
                store.put("segments", f"sess_{client_id}_{session}", transcript)
            elif roll < 0.9 and small_keys:
                op = "small_get"
                store.get("segments", rng.choice(small_keys))
            else:
                op = "transcript_get"
                store.get("segments", f"sess_{client_id}_{session}")
            err = 0
        except Exception as exc:  # noqa: BLE001 - counted and reported
            err = 1
            print(f"client {client_id} {op} error: {exc!r}"[:400], file=sys.stderr)
        dt = time.perf_counter() - t
        slot = minute.setdefault(int((time.monotonic() - start) // 60), {"lat": [], "err": 0})
        slot["lat"].append(dt)  # type: ignore[union-attr]
        slot["err"] = int(slot["err"]) + err  # type: ignore[call-overload]
    queue.put((client_id, {m: (s["lat"], s["err"]) for m, s in minute.items()}, appended))  # type: ignore[attr-defined]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--rss-interval", type=float, default=10.0)
    args = parser.parse_args()

    bc.use_home(args.home)
    store = bc.open_store()
    pid = bc.daemon_pid()
    deadline = time.time() + args.minutes * 60
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    procs = [
        ctx.Process(target=_client, args=(str(args.home), i, deadline, queue))
        for i in range(args.clients)
    ]
    with bc.RssSampler(pid, interval=args.rss_interval) as rss:
        for p in procs:
            p.start()
        results = [queue.get(timeout=args.minutes * 60 + 1800) for _ in procs]
        for p in procs:
            p.join(timeout=120)
    per_minute = []
    minutes = sorted({m for _c, mins, _a in results for m in mins})
    for m in minutes:
        lat = [x for _c, mins, _a in results for x in mins.get(m, ([], 0))[0]]
        err = sum(mins.get(m, ([], 0))[1] for _c, mins, _a in results)
        rss_at = [v for t, v in rss.samples if m * 60 <= t < (m + 1) * 60]
        per_minute.append(
            {
                "minute": m,
                **bc.percentiles(lat),
                "over_1s": sum(1 for x in lat if x > _SLOW_S),
                "over_15s": sum(1 for x in lat if x > _BOUND_S),
                "errors": err,
                "daemon_rss_mib": max(rss_at) if rss_at else None,
            }
        )
        print(per_minute[-1], file=sys.stderr)
    all_lat = [x for _c, mins, _a in results for v in mins.values() for x in v[0]]
    bc.write_json(
        args.out,
        {
            "iowarp_core": bc.core_version(),
            "minutes": args.minutes,
            "clients": args.clients,
            "small_appends": sum(a for _c, _m, a in results),
            "overall": bc.percentiles(all_lat),
            "over_1s": sum(1 for x in all_lat if x > _SLOW_S),
            "over_15s": sum(1 for x in all_lat if x > _BOUND_S),
            "errors": sum(e for _c, mins, _a in results for _l, e in mins.values()),
            "daemon_rss": rss.summary(),
            "per_minute": per_minute,
            "rss_series": rss.samples,
        },
    )
    print(bc.stop_daemon(store), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
