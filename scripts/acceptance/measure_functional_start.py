#!/usr/bin/env python3
"""Time a real CLIO server from launch to functional, the "Agent" row of the acceptance matrix.

Functional means: ``/v1/health`` reports ``readiness: ready``, the conversation list's
first page answers, and the newest conversation's first message page answers. Every
time is measured from the moment the server process is launched.

The server runs with the caller's environment, so point it at a store first (for a
generated reference store: ``source <store>/env.sh`` on Linux/macOS, or set the same
variables in PowerShell). Leave no model provider configured unless the run should
spend model calls: with a provider, the obligations lane resumes the store's
interrupted work through it.

Usage::

    python scripts/acceptance/measure_functional_start.py \\
        --clio-agent <install>/clio-agent/.venv/bin/clio-agent --port 19101 --out agent.json

On Windows the executable is ``<install>\\clio-agent\\.venv\\Scripts\\clio-agent.exe``.
Exit status is 0 when the server became functional within ``--timeout``, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any


def _get(base: str, path: str, timeout: float) -> Any:
    with urllib.request.urlopen(base + path, timeout=timeout) as response:
        return json.loads(response.read())


def _stop(proc: subprocess.Popen[bytes], grace: float) -> bool:
    """Ask the server to stop; return whether it exited within ``grace`` seconds."""
    if os.name == "nt":
        proc.send_signal(signal.CTRL_BREAK_EVENT)  # the new process group's console
    else:
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(grace)
        return True
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        return False


def measure(clio_agent: str, port: int, timeout: float, log_path: Path) -> dict[str, Any]:
    """Launch ``clio-agent serve`` and time it to functional."""
    base = f"http://127.0.0.1:{port}"
    flags: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    result: dict[str, Any] = {"readiness_seen": []}
    with log_path.open("w") as log:
        started = time.monotonic()
        proc = subprocess.Popen(
            [clio_agent, "serve", "--host", "127.0.0.1", "--port", str(port)],
            stdout=log,
            stderr=subprocess.STDOUT,
            **flags,
        )
        elapsed = lambda: round(time.monotonic() - started, 3)  # noqa: E731
        try:
            last = None
            while time.monotonic() - started < timeout:
                try:
                    health = _get(base, "/v1/health", 5)
                except OSError:
                    time.sleep(0.25)
                    continue
                result.setdefault("first_answer_s", elapsed())
                readiness = health.get("readiness")
                if readiness != last:
                    result["readiness_seen"].append([elapsed(), readiness, health.get("starting")])
                    last = readiness
                if readiness == "ready":
                    result["ready_s"] = elapsed()
                    break
                time.sleep(0.25)
            if "ready_s" not in result:
                result["outcome"] = "not_ready_within_timeout"
                return result
            mark = time.monotonic()
            page = _get(base, "/v1/sessions?limit=30", 30)
            result["rail_page_s"] = round(time.monotonic() - mark, 3)
            rows = page.get("sessions") or []
            result["rail_rows"], result["rail_total"] = len(rows), page.get("total")
            if rows:
                mark = time.monotonic()
                _get(base, f"/v1/sessions/{rows[0]['id']}/messages?limit=50", 30)
                result["open_first_page_s"] = round(time.monotonic() - mark, 3)
            result["functional_s"] = elapsed()
            result["outcome"] = "functional"
            return result
        finally:
            mark = time.monotonic()
            result["stopped_gracefully"] = _stop(proc, 30)
            result["stop_s"] = round(time.monotonic() - mark, 3)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--clio-agent", required=True, help="the installed clio-agent executable")
    parser.add_argument("--port", type=int, default=19101)
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for ready")
    parser.add_argument("--out", type=Path, required=True, help="result JSON (log beside it)")
    args = parser.parse_args()
    result = measure(args.clio_agent, args.port, args.timeout, args.out.with_suffix(".server.log"))
    args.out.write_text(json.dumps(result, indent=1))
    print(json.dumps(result))
    return 0 if result.get("outcome") == "functional" else 1


if __name__ == "__main__":
    sys.exit(main())
