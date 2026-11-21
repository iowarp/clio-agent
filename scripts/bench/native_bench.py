"""Run the bundled ``clio_cte_bench`` / ``clio_run_thrpt_bench`` against a CLIO home's daemon.

The daemon is the one CLIO spawns for ``--home`` (its generated ``cte.yaml``), and the
bench binaries get the same child environment CLIO gives the daemon
(``pin_core_child_env``: ``USER``, loopback bind, ``CLIO_SERVER_CONF``), so they attach
as one more client. Each run's stdout is kept; the summary lines are parsed loosely
(the formats differ by version) and the raw text is stored beside them.

Usage::

    <venv>/bin/python scripts/bench/native_bench.py --home .bench/native250 \
        --out .bench/results/native_2.5.0.json
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _bench_common as bc  # noqa: E402

CTE_CASES = [
    # (op, threads, depth, io_size, io_count)
    ("Put", 1, 1, "4k", 4000),
    ("Get", 1, 1, "4k", 4000),
    ("Put", 4, 4, "4k", 4000),
    ("Get", 4, 4, "4k", 4000),
    ("Put", 1, 1, "1m", 400),
    ("Get", 1, 1, "1m", 400),
    ("Put", 4, 4, "1m", 200),
    ("Get", 4, 4, "1m", 200),
]
THRPT_CASES = [("latency", 1, "4k"), ("latency", 4, "4k")]
#: The result lines both benches print (clio_cte_bench reports through INFO logging).
_RESULT = re.compile(
    r"(Aggregate bandwidth|Aggregate IOPS|Avg latency per op|Throughput:|Avg round-trip)"
)


def _env(store: object) -> dict[str, str]:
    import iowarp_core  # noqa: PLC0415

    from clio_agent.arc.clio_core_config import pin_core_child_env  # noqa: PLC0415
    from clio_agent.arc.runtime_spawn import _dynamic_library_env_var  # noqa: PLC0415

    env = os.environ.copy()
    pin_core_child_env(env)
    env["CLIO_SERVER_CONF"] = store._config_path  # type: ignore[attr-defined]
    var = _dynamic_library_env_var()
    env[var] = iowarp_core.get_lib_dir() + os.pathsep + env.get(var, "")
    env["CTP_LOG_LEVEL"] = "info"
    return env


def _run(argv: list[str], env: dict[str, str]) -> dict[str, object]:
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=900, check=False)
    text = re.sub(r"\x1b\[[0-9;]*m", "", done.stdout + done.stderr)
    tail = [ln.split("PrintResults ")[-1] for ln in text.splitlines() if _RESULT.search(ln)]
    return {"argv": argv[1:], "rc": done.returncode, "summary": tail, "raw_tail": text[-3000:]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    import iowarp_core  # noqa: PLC0415

    bc.use_home(args.home)
    store = bc.open_store()
    bin_dir = Path(iowarp_core.get_bin_dir())
    env = _env(store)
    results: dict[str, list[dict[str, object]]] = {"clio_cte_bench": [], "clio_run_thrpt_bench": []}
    try:
        for op, threads, depth, size, count in CTE_CASES:
            argv = [str(bin_dir / "clio_cte_bench"), "--op", op, "--threads", str(threads)]
            argv += ["--depth", str(depth), "--io-size", size, "--io-count", str(count)]
            results["clio_cte_bench"].append(_run(argv, env))
            print(results["clio_cte_bench"][-1]["summary"], file=sys.stderr)
        for case, threads, size in THRPT_CASES:
            argv = [str(bin_dir / "clio_run_thrpt_bench"), "--test-case", case]
            argv += ["--threads", str(threads), "--io-size", size, "--duration", "10"]
            argv += ["--output-dir", str(args.home / "thrpt")]
            results["clio_run_thrpt_bench"].append(_run(argv, env))
            print(results["clio_run_thrpt_bench"][-1]["summary"], file=sys.stderr)
    finally:
        print(bc.stop_daemon(store), file=sys.stderr)
    bc.write_json(args.out, {"iowarp_core": bc.core_version(), **results})
    return 0


if __name__ == "__main__":
    sys.exit(main())
