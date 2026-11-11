"""A plain sensor feed for the stream checks: a room sensor that changes every second.

``GET /reading`` answers the sensor's current reading as JSON (``t``, ``temperature_c``,
``humidity_pct``, ``status``); the temperature drifts on a slow wave with a warm spike
every ``--spike-s`` seconds (``status`` reads ``warm`` while it lasts), so "the highest
temperature in the last minute" has a definite answer. ``GET /log`` answers one new text
line per call (an event log). ``GET /`` describes the endpoints.

    python scripts/cloud/fixtures/sensor_feed.py --port 8798
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = time.monotonic()
_LOG_SEQ = itertools.count(1)
_LOG_LOCK = threading.Lock()
_EVENTS = ("door opened", "door closed", "fan on", "fan off", "light on", "light off")


def reading(spike_s: float) -> dict[str, object]:
    """The sensor at this second: a slow wave, a warm spike once every ``spike_s``."""
    second = int(time.monotonic() - START)
    phase = second % spike_s
    spike = 4.0 if phase < 5 else 0.0
    return {
        "t": round(time.time(), 3),
        "temperature_c": round(21.0 + 1.2 * math.sin(second / 20.0) + spike, 2),
        "humidity_pct": round(45.0 + 5.0 * math.cos(second / 30.0), 1),
        "status": "warm" if spike else "ok",
    }


def log_line() -> str:
    """The next event-log line."""
    with _LOG_LOCK:
        seq = next(_LOG_SEQ)
    return f"{time.strftime('%H:%M:%S')} #{seq} {_EVENTS[seq % len(_EVENTS)]}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8798)
    parser.add_argument("--spike-s", type=float, default=45.0)
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the http.server interface
            path = self.path.split("?")[0]
            if path == "/reading":
                data, kind = json.dumps(reading(args.spike_s)).encode(), "application/json"
            elif path == "/log":
                data, kind = (log_line() + "\n").encode(), "text/plain"
            else:
                body = {
                    "endpoints": {
                        "/reading": "the sensor's current reading as JSON",
                        "/log": "the next line of the sensor's event log, as text",
                    }
                }
                data, kind = json.dumps(body).encode(), "application/json"
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: object) -> None:
            return

    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
