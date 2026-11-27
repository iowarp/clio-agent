"""CLIO's ARC path with each iowarp-core 2.5 capability on and off (one venv, one machine).

Modes (each on its own fresh CLIO home, run one after another):

* ``async``: CLIO's default (``Async*`` + progress-bounded wait).
* ``sync``: ``arc.clio_core.blob_calls=sync`` (``Client.PutBlob/GetBlob/GetBlobSize``).
* ``sync_shm_md``: ``sync`` plus a ``clio_cte_core_shm_cache_writethru`` pool composed
  in ``md_only`` mode between CLIO's indexer and the core (``arc.cte.shm_cache=md_only``).
* ``async_shm_md``: the same compose with the default async calls.

For each mode it runs :mod:`arc_store_bench` (payload sweep and 1/4/16-client
concurrency), the cold start and the daemon's idle CPU, and records whether the client
connected to the shm cache. Usage::

    .venv/bin/python scripts/bench/adoption_bench.py --root .bench/adopt \
        --out .bench/results/adoption_2.5.0.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bench_common as bc  # noqa: E402

HERE = Path(__file__).resolve().parent
MODES = {
    "async": {"CLIO_ARC_CLIO_CORE_BLOB_CALLS": "async"},
    "sync": {"CLIO_ARC_CLIO_CORE_BLOB_CALLS": "sync"},
    "sync_shm_md": {"CLIO_ARC_CLIO_CORE_BLOB_CALLS": "sync", "CLIO_ARC_CTE_SHM_CACHE": "md_only"},
    "async_shm_md": {
        "CLIO_ARC_CLIO_CORE_BLOB_CALLS": "async",
        "CLIO_ARC_CTE_SHM_CACHE": "md_only",
    },
}

_PROBE = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
import _bench_common as bc
t0 = time.perf_counter()
store = bc.open_store()
t1 = time.perf_counter()
pid = bc.daemon_pid()
has_cache = bool(getattr(store._client, "HasCache", lambda: False)())
import idle_and_metadata_bench as idle  # the daemon is released when this process exits
cpu = idle.idle_cpu(pid, 20.0, 30.0) if pid else {}
rss = bc._rss_breakdown(pid)
print("RESULT " + json.dumps({"attach_s": t1 - t0, "pid": pid, "has_cache": has_cache,
      "config": open(store._config_path).read(), "idle_cpu": cpu, "idle_rss": rss}))
"""


def _run(argv: list[str], env: dict[str, str], timeout: float) -> str:
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(f"{argv[1]} rc={done.returncode}: {done.stderr[-2000:]}")
    return done.stdout + done.stderr


def run_mode(name: str, home: Path, out_dir: Path, sizes: str, conc_sizes: str) -> dict[str, Any]:
    shutil.rmtree(home, ignore_errors=True)
    env = {**os.environ, "CLIO_AGENT_HOME": str(home.resolve()), "CTP_LOG_LEVEL": "error"}
    env.pop("CLIO_USER_DIR", None)
    env.update(MODES[name])
    home.mkdir(parents=True)
    text = _run([sys.executable, "-c", _PROBE, str(HERE)], env, 300)
    probe = json.loads(next(ln for ln in text.splitlines() if ln.startswith("RESULT "))[7:])
    store_json = out_dir / f"adoption_store_{name}.json"
    argv = [sys.executable, str(HERE / "arc_store_bench.py"), "--home", str(home)]
    argv += ["--out", str(store_json), "--sizes", sizes, "--concurrency-sizes", conc_sizes]
    _run(argv, env, 3600)  # stops the daemon when done
    store = json.loads(store_json.read_text())
    return {
        "mode": name,
        "env": MODES[name],
        "cold_start_attach_ms": round(probe["attach_s"] * 1000, 1),
        "client_has_shm_cache": probe["has_cache"],
        "shm_cache_composed": "clio_cte_core_shm_cache_writethru" in probe["config"],
        "idle_cpu": probe["idle_cpu"],
        "idle_rss_mib": probe["idle_rss"],
        "sweep": store["sweep"],
        "concurrency": store["concurrency"],
        "daemon_rss": store["daemon_rss"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--sizes", default="1K,4K,64K,1M,16M")
    parser.add_argument("--concurrency-sizes", default="4K,1M")
    args = parser.parse_args()
    rows = []
    try:
        for name in args.modes.split(","):
            t0 = time.monotonic()
            rows.append(
                run_mode(
                    name, args.root / name, args.out.parent, args.sizes, args.concurrency_sizes
                )
            )
            print(f"{name}: done in {time.monotonic() - t0:.0f}s", file=sys.stderr)
    finally:
        shutil.rmtree(args.root, ignore_errors=True)
    bc.write_json(args.out, {"iowarp_core": bc.core_version(), "modes": rows})
    return 0


if __name__ == "__main__":
    sys.exit(main())
