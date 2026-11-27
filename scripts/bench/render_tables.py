"""Render the JSON results of ``compare_versions.sh`` as Markdown tables.

Usage::

    python scripts/bench/render_tables.py .bench/results 2.4.1 2.5.0 > tables.md

Missing result files are reported as a line, not skipped silently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _load(out: Path, kind: str, ver: str) -> dict[str, Any] | None:
    path = out / f"{kind}_{ver}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _missing(kind: str, vers: list[str], data: dict[str, Any]) -> list[str]:
    return [f"_{kind}: no result for {v}_" for v in vers if data.get(v) is None]


def _size(n: int) -> str:
    for unit, scale in (("M", 2**20), ("K", 2**10)):
        if n >= scale:
            return f"{n // scale}{unit}"
    return str(n)


def lifecycle(out: Path, vers: list[str]) -> list[str]:
    data = {v: _load(out, "lifecycle", v) for v in vers}
    rows = [
        ("Cold start + attach", "cold_start_attach"),
        ("Attach to running daemon", "warm_attach"),
        ("Restart + attach, existing store", "restart_attach_existing_store"),
        ("Clean stop after 64 KiB x 2000 fill", "clean_stop_after_fill"),
    ]
    lines = ["| Phase (ms, 5 reps) | " + " | ".join(f"{v} p50 / max" for v in vers) + " |"]
    lines.append("|---" * (len(vers) + 1) + "|")
    for label, key in rows:
        cells = []
        for v in vers:
            d = (data[v] or {}).get(key)
            cells.append(f"{d['p50_ms']:.0f} / {d['max_ms']:.0f}" if d else "n/a")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    return lines + _missing("lifecycle", vers, data)


def sweep(out: Path, vers: list[str]) -> list[str]:
    data = {v: _load(out, "arc_store", v) for v in vers}
    head = "| Size | " + " | ".join(
        f"{v} put p50/p95/p99 ms | {v} get p50/p95/p99 ms | {v} put/get MiB/s" for v in vers
    )
    lines = [head + " |", "|---" * (1 + 3 * len(vers)) + "|"]
    sizes = [
        r["size_bytes"] for r in (next((d for d in data.values() if d), {}) or {}).get("sweep", [])
    ]
    for size in sizes:
        cells = []
        for v in vers:
            row = next(
                (r for r in (data[v] or {}).get("sweep", []) if r["size_bytes"] == size), None
            )
            if row is None:
                cells += ["n/a"] * 3
                continue
            p, g = row["put"], row["get"]
            err = f" ({row['errors']} err)" if row["errors"] else ""
            cells.append(f"{p['p50_ms']:.2f} / {p['p95_ms']:.2f} / {p['p99_ms']:.2f}{err}")
            cells.append(f"{g['p50_ms']:.2f} / {g['p95_ms']:.2f} / {g['p99_ms']:.2f}")
            cells.append(f"{row['put_mib_s']:.1f} / {row['get_mib_s']:.1f}")
        lines.append(f"| {_size(size)} | " + " | ".join(cells) + " |")
    return lines + _missing("arc_store", vers, data)


def concurrency(out: Path, vers: list[str]) -> list[str]:
    data = {v: _load(out, "arc_store", v) for v in vers}
    head = "| Size x clients | " + " | ".join(
        f"{v} put p50/p99 ms | {v} get p50/p99 ms | {v} put/get ops/s" for v in vers
    )
    lines = [head + " |", "|---" * (1 + 3 * len(vers)) + "|"]
    base = (next((d for d in data.values() if d), {}) or {}).get("concurrency", [])
    for ref in base:
        key = (ref["size_bytes"], ref["clients"])
        cells = []
        for v in vers:
            row = next(
                (
                    r
                    for r in (data[v] or {}).get("concurrency", [])
                    if (r["size_bytes"], r["clients"]) == key
                ),
                None,
            )
            if row is None:
                cells += ["n/a"] * 3
                continue
            p, g = row["put"], row["get"]
            err = f" ({row['errors']} err)" if row["errors"] else ""
            cells.append(f"{p['p50_ms']:.2f} / {p['p99_ms']:.2f}{err}")
            cells.append(f"{g['p50_ms']:.2f} / {g['p99_ms']:.2f}")
            cells.append(f"{row['put_ops_s']:.0f} / {row['get_ops_s']:.0f}")
        lines.append(f"| {_size(key[0])} x{key[1]} | " + " | ".join(cells) + " |")
    rss = []
    for v in vers:
        r = (data[v] or {}).get("daemon_rss", {})
        if r.get("samples"):
            rss.append(f"{v}: {r['start_mib']} → peak {r['peak_mib']} → {r['end_mib']} MiB")
    if rss:
        lines += ["", "Daemon RSS across the store bench: " + "; ".join(rss) + "."]
    return lines + _missing("arc_store", vers, data)


def native(out: Path, vers: list[str]) -> list[str]:
    data = {v: _load(out, "native", v) for v in vers}
    lines = ["| Bench | Case | " + " | ".join(vers) + " |", "|---" * (2 + len(vers)) + "|"]
    for bench in ("clio_cte_bench", "clio_run_thrpt_bench"):
        ref = next((d for d in data.values() if d), {}) or {}
        for i, case in enumerate(ref.get(bench, [])):
            argv = case["argv"]
            label = " ".join(a for a in argv if not a.startswith("/"))
            cells = []
            for v in vers:
                runs = (data[v] or {}).get(bench, [])
                run = runs[i] if i < len(runs) else None
                if run is None:
                    cells.append("n/a")
                elif run["rc"] != 0:
                    cells.append(f"rc {run['rc']}")
                else:
                    cells.append("<br>".join(run["summary"]) or "no result line")
            lines.append(f"| {bench} | `{label}` | " + " | ".join(cells) + " |")
    return lines + _missing("native", vers, data)


def soak(out: Path, vers: list[str]) -> list[str]:
    data = {v: _load(out, "soak", v) for v in vers}
    lines = ["| Soak | " + " | ".join(vers) + " |", "|---" * (1 + len(vers)) + "|"]

    def cell(v: str, fn: Any) -> str:
        d = data[v]
        return "n/a" if d is None else fn(d)

    def minute(d: dict[str, Any], idx: int, key: str) -> str:
        pm = d["per_minute"]
        return f"{pm[idx][key]:.2f}" if pm else "n/a"

    rows = [
        ("Minutes x clients", lambda d: f"{d['minutes']:g} x {d['clients']}"),
        ("Ops (small appends)", lambda d: f"{d['overall']['n']} ({d['small_appends']})"),
        (
            "Latency p50 / p99 / max ms",
            lambda d: "{p50_ms:.2f} / {p99_ms:.2f} / {max_ms:.0f}".format(**d["overall"]),
        ),
        (
            "p99 ms, first minute → last minute",
            lambda d: f"{minute(d, 0, 'p99_ms')} → {minute(d, -1, 'p99_ms')}",
        ),
        (
            "Ops/min, first → last",
            lambda d: f"{d['per_minute'][0]['n']} → {d['per_minute'][-1]['n']}"
            if d["per_minute"]
            else "n/a",
        ),
        ("Ops over 1 s / over 15 s", lambda d: f"{d['over_1s']} / {d['over_15s']}"),
        ("Errors", lambda d: str(d["errors"])),
        (
            "Daemon RSS start → peak → end MiB",
            lambda d: "{start_mib} → {peak_mib} → {end_mib}".format(**d["daemon_rss"])
            if d["daemon_rss"].get("samples")
            else "n/a",
        ),
    ]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(cell(v, fn) for v in vers) + " |")
    return lines + _missing("soak", vers, data)


def main() -> int:
    out, vers = Path(sys.argv[1]), sys.argv[2:]
    sections = [
        ("Daemon lifecycle", lifecycle),
        ("ARC store payload sweep (1 client)", sweep),
        ("ARC store concurrency (client processes on one daemon)", concurrency),
        ("Bundled native benches", native),
        ("Soak (#1846)", soak),
    ]
    for title, fn in sections:
        print(f"### {title}\n")
        print("\n".join(fn(out, vers)))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
