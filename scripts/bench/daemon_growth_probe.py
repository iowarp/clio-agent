"""What grows the clio-core daemon's anonymous memory: stored blobs or operations?

One private CLIO home, one client. Three phases, each followed by a settle and a
reading of the daemon's ``RssAnon`` (Linux ``/proc``):

* ``overwrite``: ``--ops`` puts of ``--size`` bytes rotating over ``--names`` blobs
  (the blob count stays fixed; only operations accumulate);
* ``new_blobs``: ``--ops`` puts of ``--size`` bytes, each to a new blob;
* ``gets``: ``--ops`` gets of the overwrite blobs.

Reports anonymous MiB per phase and per 1,000 ops / per 1,000 new blobs. Usage::

    <venv>/bin/python scripts/bench/daemon_growth_probe.py --home .bench/growth250 \
        --out .bench/results/growth_2.5.0.json
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bench_common as bc  # noqa: E402


def _anon(pid: int) -> float:
    return bc._rss_breakdown(pid).get("RssAnon", -1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ops", type=int, default=40_000)
    parser.add_argument("--names", type=int, default=100)
    parser.add_argument("--size", type=int, default=4096)
    args = parser.parse_args()

    bc.use_home(args.home)
    store = bc.open_store()
    pid = bc.daemon_pid()
    payload = os.urandom(args.size)
    rows: dict[str, object] = {"ops": args.ops, "names": args.names, "size": args.size}
    try:

        def phase(label: str, op) -> None:  # noqa: ANN001
            before = _anon(pid)
            t0 = time.perf_counter()
            for i in range(args.ops):
                op(i)
            elapsed = time.perf_counter() - t0
            time.sleep(5)
            after = _anon(pid)
            rows[label] = {
                "anon_before_mib": before,
                "anon_after_mib": after,
                "anon_growth_mib": round(after - before, 1),
                "kib_per_op": round((after - before) * 1024 / args.ops, 3),
                "seconds": round(elapsed, 1),
            }
            print(label, rows[label], file=sys.stderr)

        phase("overwrite", lambda i: store.put("segments", f"ow_{i % args.names}", payload))
        phase("new_blobs", lambda i: store.put("segments", f"nb_{i}", payload))
        phase("gets", lambda i: store.get("segments", f"ow_{i % args.names}"))
    finally:
        print(bc.stop_daemon(store), file=sys.stderr)
    bc.write_json(args.out, {"iowarp_core": bc.core_version(), **rows})
    return 0


if __name__ == "__main__":
    sys.exit(main())
