"""Measure the gact server's resident memory versus the number of stored sessions.

This is a *measurement* harness for iowarp/clio-agent#893 (resource-usage
campaign, step 2). It answers one question with numbers instead of assumptions:

    How does the gact server's RSS scale with the count of resident sessions,
    given that ``build_app`` eagerly loads **every** session's message ledger
    into ``app.state.messages`` at boot (``MessageStore.load_all`` — see
    ``src/clio_agent/gact/app.py:1293``)?

Method
------
For each requested session count ``N`` the harness:

1. Synthesises a **temporary** on-disk store (never touches the real ``.clio``):
   a ``sessions.json`` registry plus one ``messages/<sid>.json`` per session,
   each a byte-copy of a real rich template session (default: the 891 run
   ``sess_c7fbe367da29``, 135 parts / ~1.25 MB).
2. Boots the **real** gact server as a uvicorn subprocess pointed at that store
   via ``CLIO_SESSIONS_PATH`` (agent-less: no ``CLIO_LM_PROVIDER``, so RSS
   reflects the message-ledger residency, not an LM/ARC hydration). The server
   runs with ``CLIO_ARC_STORE=local`` so the clio-core CTE daemon is never
   involved.
3. Waits for ``GET /v1/health`` to answer (boot-to-healthy time; the eager
   ``load_all`` runs inside ``build_app`` *before* the port binds, so a healthy
   response means every ledger is already resident).
4. Measures a settled RSS (after a ``GET /v1/sessions``), then fetches
   ``GET /v1/sessions/{sid}/messages`` for up to 10 sessions and measures RSS
   again — testing whether *reading* messages grows residency (it should not:
   the reads are served from the already-resident ``app.state.messages``).
5. Kills the server before the next run.

The harness **fails loudly** if a server never becomes healthy; there are no
silent skips.

Outputs a table (boot behavior, RSS vs N, boot time, post-read RSS) plus the
fitted slopes, and writes the raw measurements to a JSON file.

Usage
-----
    uv run python scripts/profile_session_memory.py \
        --template-session .clio/agent/messages/sess_c7fbe367da29.json \
        --counts 0,50,200,500 --port 18800 --out /tmp/893_profile.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

# ----------------------------------------------------------------------------- #
# Measurement record
# ----------------------------------------------------------------------------- #


@dataclass
class RunResult:
    """One (session-count) measurement point.

    Attributes:
        count: Number of resident sessions the server was booted against.
        boot_seconds: Wall time from subprocess spawn to first healthy
            ``GET /v1/health`` response (includes import + eager ``load_all``).
        rss_boot_mb: Settled RSS (MiB) after boot + one ``GET /v1/sessions``.
        rss_after_read_mb: RSS (MiB) after fetching messages for up to 10
            sessions — probes whether reads grow residency.
        rss_baseline_import_mb: RSS (MiB) of the process, recorded at boot; the
            N=0 point serves as the fixed import/framework baseline.
        sessions_listed: Count returned by ``GET /v1/sessions`` (sanity check).
        messages_fetched: Number of sessions whose messages were fetched.
    """

    count: int
    boot_seconds: float
    rss_boot_mb: float
    rss_after_read_mb: float
    sessions_listed: int
    messages_fetched: int


# ----------------------------------------------------------------------------- #
# Temp store synthesis
# ----------------------------------------------------------------------------- #


def _session_record(sid: str, template_rec: dict[str, Any], msg_count: int) -> dict[str, Any]:
    """Return a valid ``Session`` dict for ``sid`` cloned from the template record."""

    rec = dict(template_rec)
    rec["id"] = sid
    rec["title"] = f"synthetic {sid}"
    rec["message_count"] = msg_count
    return rec


def build_temp_store(
    root: Path,
    count: int,
    template_msg_path: Path,
    template_rec: dict[str, Any],
) -> Path:
    """Materialise a temp store with ``count`` copies of the template session.

    Layout mirrors the real store: ``<root>/sessions.json`` (the registry) and
    ``<root>/messages/<sid>.json`` (one byte-copy of the template per session).

    Args:
        root: Directory to populate (created if absent).
        count: Number of synthetic sessions to write.
        template_msg_path: Path to the real template message ledger to copy.
        template_rec: The template's ``sessions.json`` record to clone.

    Returns:
        The ``sessions.json`` path to pass as ``CLIO_SESSIONS_PATH``.
    """

    messages_dir = root / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)
    template_bytes = template_msg_path.read_bytes()
    msg_count = len(json.loads(template_bytes))

    registry: dict[str, dict[str, Any]] = {}
    for i in range(count):
        sid = f"sess_synth{i:06d}"
        (messages_dir / f"{sid}.json").write_bytes(template_bytes)
        registry[sid] = _session_record(sid, template_rec, msg_count)

    sessions_path = root / "sessions.json"
    sessions_path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    return sessions_path


# ----------------------------------------------------------------------------- #
# HTTP helpers (stdlib only — no dependency on the running server's client)
# ----------------------------------------------------------------------------- #


def _get_json(url: str, timeout: float = 10.0) -> Any:
    """GET ``url`` and decode JSON, raising on any transport/HTTP error."""

    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - localhost only
        return json.loads(resp.read().decode("utf-8"))


def _wait_healthy(base_url: str, proc: subprocess.Popen[bytes], deadline_s: float) -> float:
    """Block until ``GET /v1/health`` answers; return seconds waited.

    Raises:
        RuntimeError: if the process dies or the deadline elapses before the
            server answers — this harness must FAIL loudly, never skip.
    """

    start = time.monotonic()
    url = f"{base_url}/v1/health"
    while True:
        if proc.poll() is not None:
            raise RuntimeError(
                f"gact server exited early (code={proc.returncode}) before becoming "
                f"healthy at {url}"
            )
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:  # noqa: S310 - localhost
                if resp.status == 200:
                    return time.monotonic() - start
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        if time.monotonic() - start > deadline_s:
            raise RuntimeError(
                f"gact server never became healthy at {url} within {deadline_s:.0f}s"
            )
        time.sleep(0.25)


# ----------------------------------------------------------------------------- #
# RSS measurement
# ----------------------------------------------------------------------------- #


def _process_rss_mb(pid: int) -> float:
    """Return RSS (MiB) of ``pid`` plus all descendants (uvicorn may fork)."""

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return 0.0
    total = proc.memory_info().rss
    for child in proc.children(recursive=True):
        try:
            total += child.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total / (1024 * 1024)


# ----------------------------------------------------------------------------- #
# One measurement run
# ----------------------------------------------------------------------------- #


def run_one(
    count: int,
    port: int,
    template_msg_path: Path,
    template_rec: dict[str, Any],
    tmp_root: Path,
    boot_deadline_s: float,
    settle_s: float,
) -> RunResult:
    """Boot a server against ``count`` synthetic sessions and measure its RSS."""

    store_root = tmp_root / f"store_{count}"
    if store_root.exists():
        shutil.rmtree(store_root)
    sessions_path = build_temp_store(store_root, count, template_msg_path, template_rec)

    env = dict(os.environ)
    env["CLIO_SESSIONS_PATH"] = str(sessions_path)
    env["CLIO_ARC_STORE"] = "local"  # never involve the clio-core CTE daemon
    env.pop("CLIO_LM_PROVIDER", None)  # agent-less: isolate message-ledger residency
    # Allow the temp store + cwd so no file policy trips the boot.
    env["CLIO_ALLOWED_ROOTS"] = os.pathsep.join([str(tmp_root), str(Path.cwd())])

    base_url = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "clio_agent.gact.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        boot_seconds = _wait_healthy(base_url, proc, boot_deadline_s)
        time.sleep(settle_s)  # let the event loop settle before sampling

        sessions = _get_json(f"{base_url}/v1/sessions?include_all_workspaces=true")
        listed = sessions.get("sessions", sessions) if isinstance(sessions, dict) else sessions
        sessions_listed = len(listed) if isinstance(listed, list) else 0

        time.sleep(settle_s)
        rss_boot_mb = _process_rss_mb(proc.pid)

        # Fetch messages for up to 10 sessions — does reading grow residency?
        ids = [row["id"] for row in listed if isinstance(row, dict)][:10] if listed else []
        for sid in ids:
            _get_json(f"{base_url}/v1/sessions/{sid}/messages")
        time.sleep(settle_s)
        rss_after_read_mb = _process_rss_mb(proc.pid)

        return RunResult(
            count=count,
            boot_seconds=round(boot_seconds, 3),
            rss_boot_mb=round(rss_boot_mb, 2),
            rss_after_read_mb=round(rss_after_read_mb, 2),
            sessions_listed=sessions_listed,
            messages_fetched=len(ids),
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


# ----------------------------------------------------------------------------- #
# Slope + reporting
# ----------------------------------------------------------------------------- #


def _slope_bytes_per_session(points: list[tuple[int, float]]) -> float:
    """Least-squares slope (bytes/session) of RSS-MiB vs session count."""

    if len(points) < 2:
        return 0.0
    n = len(points)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0
    slope_mib_per_session = (n * sxy - sx * sy) / denom
    return slope_mib_per_session * 1024 * 1024


def _print_table(results: list[RunResult], template_bytes: int, template_parts: int) -> None:
    """Print the RSS table + fitted slopes to stdout."""

    print()
    print(f"Template session: {template_parts} parts, {template_bytes:,} bytes on disk")
    print()
    header = (
        f"{'N':>6} {'boot_s':>8} {'RSS_boot_MiB':>14} {'RSS_read_MiB':>14} "
        f"{'read_dMiB':>10} {'fetched':>8} {'listed':>7}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        read_delta = r.rss_after_read_mb - r.rss_boot_mb
        print(
            f"{r.count:>6} {r.boot_seconds:>8.2f} {r.rss_boot_mb:>14.2f} "
            f"{r.rss_after_read_mb:>14.2f} {read_delta:>10.2f} "
            f"{r.messages_fetched:>8} {r.sessions_listed:>7}"
        )

    boot_points = [(r.count, r.rss_boot_mb) for r in results]
    boot_slope = _slope_bytes_per_session(boot_points)
    print()
    print(f"RSS-at-boot slope:        {boot_slope:>12,.0f} bytes/session (resident-at-boot cost)")
    # The read probe fetches only min(N, 10) sessions, so the read cost is a
    # per-FETCHED-session figure, not a per-N slope. Average it over the runs
    # that actually fetched something.
    read_costs = [
        ((r.rss_after_read_mb - r.rss_boot_mb) * 1024 * 1024) / r.messages_fetched
        for r in results
        if r.messages_fetched > 0
    ]
    if read_costs:
        avg_read = sum(read_costs) / len(read_costs)
        print(f"read delta per fetched:   {avg_read:>12,.0f} bytes/fetched-session (transient)")
    base = next((r.rss_boot_mb for r in results if r.count == 0), None)
    if base is not None:
        print(f"import/framework baseline (N=0): {base:.2f} MiB")


# ----------------------------------------------------------------------------- #
# Entry point
# ----------------------------------------------------------------------------- #


def main() -> None:
    """Parse args, run the sweep, print the table, and write JSON."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template-session",
        type=Path,
        default=Path(".clio/agent/messages/sess_c7fbe367da29.json"),
        help="Path to a real message ledger to clone as the template session.",
    )
    parser.add_argument(
        "--template-sessions-json",
        type=Path,
        default=Path(".clio/agent/sessions.json"),
        help="sessions.json holding the template's registry record.",
    )
    parser.add_argument(
        "--counts",
        default="0,50,200,500",
        help="Comma-separated session counts to sweep.",
    )
    parser.add_argument("--port", type=int, default=18800, help="Base port (18800+ range).")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("session_memory_profile.json"),
        help="Where to write raw measurements as JSON.",
    )
    parser.add_argument(
        "--tmp-root",
        type=Path,
        default=None,
        help="Directory for temp stores (default: a sibling of --out).",
    )
    parser.add_argument("--boot-deadline", type=float, default=120.0)
    parser.add_argument("--settle", type=float, default=1.5)
    args = parser.parse_args()

    template_msg_path = args.template_session.resolve()
    if not template_msg_path.exists():
        raise SystemExit(f"template session not found: {template_msg_path}")
    template_bytes = template_msg_path.read_bytes()
    template_size = len(template_bytes)
    template_parts = sum(len(m.get("parts", [])) for m in json.loads(template_bytes))

    registry = json.loads(args.template_sessions_json.read_text(encoding="utf-8"))
    template_sid = template_msg_path.stem
    template_rec = registry.get(template_sid) or next(iter(registry.values()))

    tmp_root = args.tmp_root or (args.out.resolve().parent / "_session_mem_stores")
    tmp_root.mkdir(parents=True, exist_ok=True)

    counts = [int(c) for c in args.counts.split(",") if c.strip()]
    results: list[RunResult] = []
    try:
        for i, count in enumerate(counts):
            port = args.port + i
            print(f"[run] N={count} port={port} ...", flush=True)
            results.append(
                run_one(
                    count=count,
                    port=port,
                    template_msg_path=template_msg_path,
                    template_rec=template_rec,
                    tmp_root=tmp_root,
                    boot_deadline_s=args.boot_deadline,
                    settle_s=args.settle,
                )
            )
    finally:
        # Clean up the (potentially large) synthetic stores.
        shutil.rmtree(tmp_root, ignore_errors=True)

    _print_table(results, template_size, template_parts)

    payload = {
        "template": {
            "path": str(template_msg_path),
            "bytes": template_size,
            "parts": template_parts,
        },
        "runs": [asdict(r) for r in results],
    }
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
