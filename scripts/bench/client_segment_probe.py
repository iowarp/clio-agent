"""How ``CLIO_CLIENT_DATA_SEGMENT_SIZE`` (iowarp-core 2.5, clio-core#1359) bounds CLIO.

For each segment size, a fresh private CLIO home: one client puts and reads back blobs
of growing size (does a put larger than the segment fail, and how?), then four client
processes each write ``--fill`` of 64 KiB blobs, and the daemon's shared-memory RSS
(``RssShmem``, where every client's mapped segment is counted) is read.

Usage::

    .venv/bin/python scripts/bench/client_segment_probe.py --root .bench/seg \
        --out .bench/results/client_segment_2.5.0.json
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bench_common as bc  # noqa: E402

PUT_SIZES = ["1M", "16M", "48M", "60M", "100M", "200M"]


def _writer(home: str, client: int, fill: int, barrier: Any) -> None:
    bc.use_home(Path(home))
    store = bc.open_store()
    blob = os.urandom(64 * 1024)
    barrier.wait()
    for i in range(fill):
        store.put("segments", f"w{client}_{i}", blob)


def run(segment: str, home: Path, fill: int) -> dict[str, Any]:
    shutil.rmtree(home, ignore_errors=True)
    os.environ["CLIO_CLIENT_DATA_SEGMENT_SIZE"] = segment  # inherited by the daemon too
    bc.use_home(home)
    store = bc.open_store()
    pid = bc.daemon_pid()
    puts = []
    try:
        for text in PUT_SIZES:
            size = bc.parse_size(text)
            data = os.urandom(size)
            t0 = time.perf_counter()
            try:
                store.put("segments", f"big_{text}", data)
                got = store.get("segments", f"big_{text}")
                outcome = "ok" if got == data else f"read back {len(got or b'')} bytes"
            except Exception as exc:  # noqa: BLE001 - the outcome is the measurement
                outcome = f"{type(exc).__name__}: {exc}"[:300]
            puts.append({"size": text, "outcome": outcome, "s": round(time.perf_counter() - t0, 2)})
            print(segment, puts[-1], file=sys.stderr)
        ctx = mp.get_context("spawn")
        barrier = ctx.Barrier(4)
        procs = [ctx.Process(target=_writer, args=(str(home), c, fill, barrier)) for c in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=1800)
        parts = bc._rss_breakdown(pid)
    finally:
        print(bc.stop_daemon(store), file=sys.stderr)
        shutil.rmtree(home, ignore_errors=True)
    return {"segment": segment, "puts": puts, "four_writer_rss_mib": parts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--segments", default="512MB,128MB,64MB")
    parser.add_argument("--fill", type=int, default=4000, help="64 KiB blobs per writer")
    parser.add_argument("--one", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.one:  # one segment size per process: the native attach happens once
        bc.write_json(args.out, run(args.one, args.root / args.one, args.fill))
        return 0
    rows = []
    for segment in args.segments.split(","):
        part = args.out.with_name(f"{args.out.stem}_{segment}.json")
        argv = [sys.executable, __file__, "--root", str(args.root), "--out", str(part)]
        subprocess.run([*argv, "--fill", str(args.fill), "--one", segment], check=True)
        rows.append(json.loads(part.read_text()))
        part.unlink()
    bc.write_json(args.out, {"iowarp_core": bc.core_version(), "rows": rows})
    return 0


if __name__ == "__main__":
    sys.exit(main())
