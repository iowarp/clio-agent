"""Daemon cold start, warm attach and restart-on-existing-store times.

Each measurement is a fresh Python process that imports CLIO and calls
``make_arc_store`` (the timer covers only that call; the import is reported apart):

* cold start: a new, empty home, so CLIO generates the config and spawns the daemon;
* warm attach: a second process while that daemon is running;
* restart: the daemon is stopped cleanly, then a new process starts it again on the
  existing store (metadata log replay included), after ``--fill`` records were written.

Usage::

    <venv>/bin/python scripts/bench/daemon_lifecycle_bench.py --root .bench/life250 \
        --reps 5 --out .bench/results/lifecycle_2.5.0.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bench_common as bc  # noqa: E402

_PROBE = r"""
import json, os, sys, time
t0 = time.perf_counter()
sys.path.insert(0, sys.argv[1])
import _bench_common as bc
from clio_agent.arc import storage  # noqa: F401
t1 = time.perf_counter()
store = bc.open_store()
t2 = time.perf_counter()
store.get("segments", "probe")
t3 = time.perf_counter()
mode = sys.argv[2]
if mode == "fill":
    blob = os.urandom(64 * 1024)
    for i in range(int(sys.argv[3])):
        store.put("segments", f"fill_{i}", blob)
out = {"import_s": t1 - t0, "attach_s": t2 - t1, "first_get_s": t3 - t2}
if mode in ("stop", "fill"):
    t4 = time.perf_counter()
    out["stop"] = str(bc.stop_daemon(store))
    out["stop_s"] = time.perf_counter() - t4
print("RESULT " + json.dumps(out))
"""


def _probe(home: Path, mode: str, fill: int = 0) -> dict[str, object]:
    env = {**os.environ, "CLIO_AGENT_HOME": str(home), "CTP_LOG_LEVEL": "error"}
    env.pop("CLIO_USER_DIR", None)
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, str(Path(__file__).resolve().parent), mode, str(fill)],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    for line in done.stdout.splitlines():
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    raise RuntimeError(f"probe {mode} failed rc={done.returncode}: {done.stderr[-2000:]}")


def _hold_attached(home: Path) -> subprocess.Popen[str]:
    """A process that stays attached, so the warm-attach probe finds a daemon."""
    env = {**os.environ, "CLIO_AGENT_HOME": str(home), "CTP_LOG_LEVEL": "error"}
    env.pop("CLIO_USER_DIR", None)
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import _bench_common as bc; "
        "s = bc.open_store(); print('READY', flush=True); sys.stdin.read(); "
        "print(bc.stop_daemon(s))"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(Path(__file__).resolve().parent)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if "READY" not in line:
        raise RuntimeError(f"holder did not attach: {line!r}")
    return proc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--fill", type=int, default=2000, help="64 KiB records before restart")
    args = parser.parse_args()
    args.root = args.root.resolve()

    cold, warm, restart, stops = [], [], [], []
    for rep in range(args.reps):
        home = args.root / f"rep{rep}"
        shutil.rmtree(home, ignore_errors=True)
        holder = _hold_attached(home)  # cold start happens inside the holder
        try:
            warm.append(_probe(home, "attach"))
        finally:
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(timeout=120)
        shutil.rmtree(home, ignore_errors=True)
        cold.append(_probe(home, "stop"))
        stops.append(_probe(home, "fill", args.fill))
        restart.append(_probe(home, "stop"))
        shutil.rmtree(home, ignore_errors=True)
        print(f"rep {rep}: cold={cold[-1]} warm={warm[-1]} restart={restart[-1]}", file=sys.stderr)

    def col(rows: list[dict[str, object]], key: str) -> dict[str, float]:
        return bc.percentiles([float(r[key]) for r in rows if key in r])  # type: ignore[arg-type]

    bc.write_json(
        args.out,
        {
            "iowarp_core": bc.core_version(),
            "reps": args.reps,
            "fill_records_64k": args.fill,
            "cold_start_attach": col(cold, "attach_s"),
            "warm_attach": col(warm, "attach_s"),
            "restart_attach_existing_store": col(restart, "attach_s"),
            "clean_stop_after_fill": col(stops, "stop_s"),
            "import": col(cold, "import_s"),
            "raw": {"cold": cold, "warm": warm, "restart": restart, "fill_stop": stops},
        },
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
