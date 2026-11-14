"""Run the HDF5 benchmark against a live CLIO over REST and record steps, tokens, time, bytes.

Arms are a server setting, so one invocation runs one arm against the server as it is:

* ``ptc``: the default tools (``run_code`` and the ``hdf5`` binding available).
* ``direct``: start the server with ``CLIO_TOOLS_WITHHELD=run_code,shell_bash`` so the
  agent can only call tools one at a time.

``--where local`` puts the ERA5 files in the run's workspace (hard links, no copy);
``--where s3`` links the public ERA5 bucket by prefix and the agent reads it by range.
Prompts are plain requests: they name the data and the question, never a tool.

    uv run --frozen --no-sync python -m scripts.eval.hdf5_bench.run \\
        --arm ptc --where local --tasks T1,T2 --n 3 --era5 /data/era5 --out .cloud-run/hdf5-bench
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from scripts.eval.hdf5_bench import ground_truth as gt
from scripts.eval.tool_calling import collect, metrics
from scripts.eval.tool_calling.http_client import GactClient

AGENT = {
    "agent_ref": "clio/clio-data",
    "agent_delta": {"add": ["clio/hdf5-ptc"], "remove": ["clio/hdf5"]},
}
S3_SOURCES = {
    "msl": ("nsf-ncar-era5/e5.oper.an.sfc/202409", "ERA5 September 2024, surface"),
    "pl": ("nsf-ncar-era5/e5.oper.an.pl/202409", "ERA5 September 2024, pressure levels"),
}
_ANSWER = (
    "End your answer with a JSON code block holding a list with one object per row, using "
    "the keys {keys}."
)
_T1 = (
    "In the ERA5 mean sea level pressure for September 2024 ({where_msl}), find the lowest "
    "pressure inside 15–35°N, 260–290°E (100–70°W) every 6 hours from 00 UTC on 24 "
    "September to 18 UTC on 27 September: its time, latitude, longitude and value in hPa. "
)
_T2 = (
    "In the ERA5 specific cloud liquid water for 26 September 2024 ({where_clwc}), find, at "
    "00, 06, 12 and 18 UTC, the largest value inside 15–35°N, 260–290°E: the pressure level "
    "(hPa), latitude, longitude and the value in g/kg. "
)
TASKS = {
    "T1": (_T1 + _ANSWER.format(keys="time, lat, lon, msl_hpa"), "T1"),
    "T2": (_T2 + _ANSWER.format(keys="time, level_hpa, lat, lon, clwc_g_per_kg"), "T2"),
}


def _where(where: str) -> dict[str, str]:
    if where == "local":
        return {
            "where_msl": f"the workspace file era5/{gt.MSL_FILE}",
            "where_clwc": f"the workspace file era5/{gt.CLWC_FILE}",
        }
    return {
        "where_msl": f"{gt.MSL_FILE} in the linked ERA5 surface source",
        "where_clwc": f"{gt.CLWC_FILE} in the linked ERA5 pressure-level source",
    }


def _workspace(client: GactClient, root: Path, where: str, era5: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    if where == "local":
        (root / "era5").mkdir(exist_ok=True)
        for name in (gt.MSL_FILE, gt.CLWC_FILE):
            target = root / "era5" / name
            if not target.exists():
                os.link(era5 / name, target)
    found = [w for w in client.workspaces() if w.get("root_path") == str(root)]
    return (found[0] if found else client.create_workspace(f"hdf5 bench {where}", str(root)))["id"]


def _link_sources(client: GactClient, wid: str, sid: str) -> None:
    rows = client.get(f"/v1/workspaces/{wid}/sources") or {}
    rows = rows.get("sources", rows) if isinstance(rows, dict) else rows
    for root, label in S3_SOURCES.values():
        source = next((r for r in rows if r.get("root") == root), None)
        if source is None:
            source = client.post(
                f"/v1/workspaces/{wid}/sources",
                {
                    "provider": "s3",
                    "root": root,
                    "label": label,
                    "mode": "read_only",
                    "configuration": {"s3_region": "us-west-2", "s3_access": "anonymous"},
                },
            )
        if not source.get("linked"):
            client.post(f"/v1/workspaces/{wid}/sources/{source['id']}/link", {"session_id": sid})
    deadline = time.time() + 300
    while time.time() < deadline:
        rows = client.get(f"/v1/workspaces/{wid}/sources") or {}
        rows = rows.get("sources", rows) if isinstance(rows, dict) else rows
        mine = [r for r in rows if r.get("root") in {v[0] for v in S3_SOURCES.values()}]
        if len(mine) == len(S3_SOURCES) and all(r.get("linked") for r in mine):
            return
        time.sleep(3)
    raise TimeoutError("ERA5 sources did not finish linking")


def _wait(client: GactClient, sid: str, timeout_s: float) -> str:
    started = time.time()
    while time.time() - started < timeout_s:
        if time.time() - started > 15 and client.session(sid).get("status") == "idle":
            return "settled"
        time.sleep(5)
    client.cancel(sid)
    return "timeout"


def _answer(text: str) -> list[dict[str, Any]] | None:
    # The answer block may end the reply without its closing fence.
    blocks = re.findall(r"```(?:json)?\s*(\[.*?\])\s*(?:```|$)", text, flags=re.S)
    for block in reversed(blocks):
        try:
            rows = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(rows, list) and all(isinstance(r, dict) for r in rows):
            return rows
    return None


def grade(
    task: str, rows: list[dict[str, Any]] | None, truth: list[dict[str, Any]]
) -> dict[str, Any]:
    """Fraction of answer-key rows matched: same time, location within 0.25°, value close."""
    if not rows:
        return {"score": 0.0, "matched": 0, "of": len(truth), "reason": "no JSON answer"}

    def key(value: Any) -> str:
        return re.sub(r"[^0-9]", "", str(value))[:10]  # yyyymmddhh

    by_time = {key(r.get("time")): r for r in rows}
    matched = 0
    for want in truth:
        got = by_time.get(key(want["time"]))
        if not got:
            continue
        try:
            near = abs(float(got["lat"]) - want["lat"]) <= 0.25 and (
                abs((float(got["lon"]) % 360) - want["lon"]) <= 0.25
            )
            if task == "T1":
                ok = near and abs(float(got["msl_hpa"]) - want["msl_hpa"]) <= 0.5
            else:
                ok = (
                    near
                    and abs(float(got["level_hpa"]) - want["level_hpa"]) < 1
                    and abs(float(got["clwc_g_per_kg"]) - want["clwc_g_per_kg"])
                    <= 0.01 * want["clwc_g_per_kg"] + 1e-4
                )
        except (KeyError, TypeError, ValueError):
            ok = False
        matched += ok
    return {"score": round(matched / len(truth), 3), "matched": matched, "of": len(truth)}


def io_time(session: dict[str, Any]) -> dict[str, float]:
    """Wall time inside top-level tool calls (union of intervals) and the number of calls."""
    calls = session.get("tool_calls") or []
    top = [c for c in calls if not c.get("parent_call_id")]
    nested = [c for c in calls if c.get("parent_call_id")]
    # Seconds in; metrics._union_ms returns milliseconds.
    spans = [
        (c["started_at"], c["ended_at"]) for c in top if c.get("started_at") and c.get("ended_at")
    ]
    return {
        "tool_ms": round(metrics._union_ms(spans), 1),
        "top_level_calls": len(top),
        "nested_calls": len(nested),
    }


def range_bytes(client: GactClient, sid: str, export: dict[str, Any]) -> dict[str, int]:
    """Bytes the run read from S3 through CLIO's range handles (from the handles' stats)."""
    handles = set(re.findall(r"rng_[A-Za-z0-9_-]{20,}", json.dumps(export)))
    total = requests = 0
    for handle in handles:
        try:
            stats = client.get(f"/v1/sessions/{sid}/source-range-stats/{handle}")
        except Exception:  # noqa: BLE001 - an expired handle has no stats; counted as missing
            continue
        total += int(stats.get("bytes_served") or 0)
        requests += int(stats.get("requests") or 0)
    return {"handles": len(handles), "bytes": total, "requests": requests}


def run_once(
    client: GactClient, args: argparse.Namespace, task: str, index: int, truth: dict
) -> dict:
    root = Path(args.work) / f"{args.where}"
    wid = _workspace(client, root, args.where, Path(args.era5))
    title = f"hdf5 bench {args.arm} {args.where} {task} run {index}"
    session = client.create_session(
        {"workspace_id": wid, "title": title, "approval_mode": "bypass", "metadata": AGENT}
    )
    sid = str(session["id"])
    if args.where == "s3":
        _link_sources(client, wid, sid)
    prompt = TASKS[task][0].format(**_where(args.where))
    started = time.time()
    for _ in range(60):
        try:
            client.send(sid, prompt)
            break
        except Exception as exc:  # noqa: BLE001 - agent still starting: retry
            if "agent_not_available" not in str(exc):
                raise
            time.sleep(5)
    state = _wait(client, sid, args.timeout)
    wall = time.time() - started
    export = client.export(sid)
    norm = collect.normalize_export(export)
    run = {
        "config": f"{args.arm}-{args.where}",
        "task": task,
        "run_index": index,
        **norm,
        "wall_s": round(wall, 1),
    }
    m = metrics.run_metrics(run)
    reply = (norm.get("replies") or [""])[-1]
    root_session = (norm.get("sessions") or [{}])[0]
    io = io_time(root_session)
    result = {
        "arm": args.arm,
        "where": args.where,
        "task": task,
        "run": index,
        "session_id": sid,
        "state": state,
        "wall_s": round(wall, 1),
        "tool_s": round(io["tool_ms"] / 1000.0, 1),
        "model_s": round(max(0.0, wall - io["tool_ms"] / 1000.0), 1),
        "steps": m["totals"]["steps"],
        "tool_calls": m["totals"]["tool_calls"],
        "top_level_calls": io["top_level_calls"],
        "nested_calls": io["nested_calls"],
        "output_tokens": m["totals"]["output_tokens"],
        "input_tokens": int((m["totals"]["uncached_per_step"] or 0) * m["totals"]["steps"]),
        "cached_input_tokens": int((m["totals"]["cached_per_step"] or 0) * m["totals"]["steps"]),
        "cost_usd": round(float((export.get("session") or {}).get("cost_usd") or 0.0), 4),
        "tool_counts": m["totals"]["tool_counts"],
        "s3": range_bytes(client, sid, export) if args.where == "s3" else None,
        "grade": grade(task, _answer(reply), truth[task]),
    }
    out = Path(args.out) / f"{args.arm}-{args.where}" / f"{task}-run{index}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--arm", choices=("ptc", "direct"), required=True)
    parser.add_argument("--where", choices=("local", "s3"), required=True)
    parser.add_argument("--tasks", default="T1,T2")
    parser.add_argument("--n", type=int, default=3)
    parser.add_argument("--era5", required=True, help="Folder with the local ERA5 files")
    parser.add_argument("--work", default=".cloud-run/hdf5-bench/workspaces")
    parser.add_argument("--out", default=".cloud-run/hdf5-bench/results")
    parser.add_argument("--timeout", type=float, default=1500)
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    args = parser.parse_args(argv)
    args.work = str(Path(args.work).resolve())
    client = GactClient(args.base_url)
    truth = {"T1": gt.track(Path(args.era5)), "T2": gt.cloud_peaks(Path(args.era5))}
    for index in range(1, args.n + 1):
        for task in args.tasks.split(","):
            r = run_once(client, args, task, index, truth)
            print(
                f"{r['arm']}-{r['where']} {task} run {index}: {r['state']} wall {r['wall_s']}s "
                f"(tools {r['tool_s']}s) steps {r['steps']} calls {r['tool_calls']} "
                f"out {r['output_tokens']} ${r['cost_usd']:.2f} score {r['grade']['score']}"
                + (f" s3 {r['s3']['bytes'] / 2**20:.1f} MB" if r["s3"] else ""),
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
