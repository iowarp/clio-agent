"""Idle daemon CPU (clio-core#1339) and metadata-log bytes per data byte (clio-core#1335).

Two measurements against one private CLIO home's daemon:

* **idle CPU**: after the attach, the daemon is left alone for ``--settle`` seconds,
  then its CPU time (user + system, all threads) is read over ``--window`` seconds.
  Reported as the fraction of one core, with the thread count and RSS.
* **metadata amplification**: the size of the metadata write-ahead log
  (``<metadata_log_path>.*``) before and after three write patterns, each on fresh
  blob names:

  - ``grow_one_blob``: one blob grown by ``--appends`` appends of ``--append-bytes``
    at increasing offsets (the #1335 16 Nov pattern: 600 x 700 B);
  - ``many_small_blobs``: the same bytes as ``--appends`` separate blobs;
  - ``clio_records``: the same count of CLIO framed records through ``store.put``.

Usage::

    <venv>/bin/python scripts/bench/idle_and_metadata_bench.py --home .bench/idle250 \
        --out .bench/results/idle_meta_2.5.0.json
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bench_common as bc  # noqa: E402


def _cpu_seconds(proc: Any) -> float:
    times = proc.cpu_times()
    return float(times.user + times.system)


def idle_cpu(pid: int, settle_s: float, window_s: float) -> dict[str, Any]:
    import psutil  # noqa: PLC0415

    proc = psutil.Process(pid)
    time.sleep(settle_s)
    start, t0 = _cpu_seconds(proc), time.monotonic()
    time.sleep(window_s)
    used, elapsed = _cpu_seconds(proc) - start, time.monotonic() - t0
    return {
        "settle_s": settle_s,
        "window_s": round(elapsed, 2),
        "cpu_s": round(used, 3),
        "cores": round(used / elapsed, 4),
        "threads": proc.num_threads(),
        "rss_mib": round(proc.memory_info().rss / 2**20, 1),
    }


def _metadata_bytes(config_path: str) -> int:
    text = Path(config_path).read_text(encoding="utf-8")
    match = re.search(r'(?m)^\s*metadata_log_path:\s*"?([^"\r\n]+)"?', text)
    if not match:
        return -1
    base = Path(match.group(1))
    return sum(p.stat().st_size for p in base.parent.glob(base.name + "*") if p.is_file())


def amplification(store: Any, appends: int, size: int) -> dict[str, Any]:
    from clio_agent.arc.clio_core_async_ops import store_put  # noqa: PLC0415

    stamp = time.time_ns()
    data = appends * size
    rows: dict[str, Any] = {"appends": appends, "append_bytes": size, "data_bytes": data}

    def measure(label: str, write: Any) -> None:
        before = _metadata_bytes(store._config_path)
        t0 = time.perf_counter()
        write()
        elapsed = time.perf_counter() - t0
        time.sleep(1.0)  # let the log writer settle
        grown = _metadata_bytes(store._config_path) - before
        rows[label] = {
            "metadata_bytes": grown,
            "metadata_per_data_byte": round(grown / data, 2),
            "seconds": round(elapsed, 3),
        }
        print(f"{label}: {rows[label]}", file=sys.stderr)

    measure(
        "grow_one_blob",
        lambda: [
            store_put(store, "segments", f"grow_{stamp}", os.urandom(size), i * size)
            for i in range(appends)
        ],
    )
    measure(
        "many_small_blobs",
        lambda: [
            store_put(store, "segments", f"small_{stamp}_{i}", os.urandom(size))
            for i in range(appends)
        ],
    )
    measure(
        "clio_records",
        lambda: [
            store.put("segments", f"rec_{stamp}_{i}", os.urandom(size)) for i in range(appends)
        ],
    )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--settle", type=float, default=30.0)
    parser.add_argument("--window", type=float, default=60.0)
    parser.add_argument("--appends", type=int, default=600)
    parser.add_argument("--append-bytes", type=int, default=700)
    args = parser.parse_args()

    bc.use_home(args.home)
    store = bc.open_store()
    pid = bc.daemon_pid()
    try:
        idle = idle_cpu(pid, args.settle, args.window) if pid else {"error": "no daemon pid"}
        print(f"idle: {idle}", file=sys.stderr)
        meta = amplification(store, args.appends, args.append_bytes)
        idle_after = idle_cpu(pid, 5.0, 30.0) if pid else {}
    finally:
        print(bc.stop_daemon(store), file=sys.stderr)
    bc.write_json(
        args.out,
        {
            "iowarp_core": bc.core_version(),
            "idle_cpu": idle,
            "idle_cpu_after_writes": idle_after,
            "metadata": meta,
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
