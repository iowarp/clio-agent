"""A fixture bioreactor for the stream demos: a fed-batch culture compressed into minutes.

``GET /status`` answers the vessel's state as JSON: the phase (``lag``, ``exponential``,
``fed-batch``, ``stationary``, then ``harvested``), the culture's optical density
(``od600``), temperature, pH, dissolved oxygen, agitation, glucose, the feed rate, the
culture age in hours, and the alarms raised now. Growth follows a logistic curve; oxygen
falls as the culture grows and the controller raises agitation to hold it; glucose runs
down until the feed starts; pH drifts down and the base pump pulls it back. ``GET /events``
answers the next line of the run's event log (text). ``GET /`` describes the endpoints. A run
of ``--hours`` culture hours takes ``--run-s`` seconds.

    python scripts/cloud/fixtures/bioreactor_feed.py --port 8797 --run-s 180
"""

from __future__ import annotations

import argparse
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = time.monotonic()
_EVENTS: list[str] = []
_EVENT_LOCK = threading.Lock()
_SEEN: set[str] = set()


def _event(key: str, text: str) -> None:
    """Log ``text`` once (the first time ``key`` happens)."""
    with _EVENT_LOCK:
        if key not in _SEEN:
            _SEEN.add(key)
            _EVENTS.append(f"{time.strftime('%H:%M:%S')} {text}")


def status(run_s: float, hours: float) -> dict[str, object]:
    """The vessel at this moment of the compressed run."""
    share = min(1.0, (time.monotonic() - START) / run_s)
    age_h = share * hours
    wobble = math.sin(age_h * 3.1)
    od = 0.1 + 11.9 / (1 + math.exp(-0.55 * (age_h - 13)))
    growth = od / 12
    feeding = age_h >= 10
    glucose = max(0.2, 20 - 19 * min(1.0, age_h / 10)) if not feeding else 0.6 + 0.3 * wobble
    feed_rate = 0.0 if not feeding else round(4 + 14 * min(1.0, (age_h - 10) / 6), 1)
    agitation = round(200 + 700 * min(1.0, growth * 1.3))
    oxygen = max(12.0, 95 - 80 * growth + 18 * (agitation - 200) / 700 + 2 * wobble)
    ph = 7.0 - 0.18 * growth + 0.05 * wobble
    temperature = 37.0 + 0.15 * wobble + (0.6 if 15 < age_h < 16.5 else 0.0)
    if share >= 1.0:
        phase = "harvested"
    elif age_h < 3:
        phase = "lag"
    elif not feeding:
        phase = "exponential"
    elif age_h < 19:
        phase = "fed-batch"
    else:
        phase = "stationary"
    alarms = []
    if oxygen < 30:
        alarms.append("dissolved oxygen below 30%")
    if temperature > 37.4:
        alarms.append("temperature above 37.4 °C")
    if ph < 6.85:
        alarms.append("pH below 6.85")
    _event(f"phase:{phase}", f"phase {phase} at {age_h:.1f} h")
    if feeding:
        _event("feed", f"feed pump started at {feed_rate} mL/h")
    for alarm in alarms:
        _event(f"alarm:{alarm}", f"ALARM {alarm}")
    return {
        "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "vessel": "BR-2 (5 L, E. coli BL21)",
        "phase": phase,
        "age_h": round(age_h, 2),
        "od600": round(od, 2),
        "temperature_c": round(temperature, 2),
        "ph": round(ph, 3),
        "dissolved_o2_pct": round(oxygen, 1),
        "agitation_rpm": agitation,
        "glucose_g_l": round(glucose, 2),
        "feed_ml_h": feed_rate,
        "alarms": alarms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8797)
    parser.add_argument("--run-s", type=float, default=180.0)
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the http.server interface
            path = self.path.split("?")[0]
            if path == "/status":
                data = json.dumps(status(args.run_s, args.hours)).encode()
                kind = "application/json"
            elif path == "/events":
                status(args.run_s, args.hours)
                with _EVENT_LOCK:
                    line = _EVENTS.pop(0) if _EVENTS else ""
                data, kind = (line + "\n").encode(), "text/plain"
            else:
                body = {
                    "endpoints": {
                        "/status": "the bioreactor's current state as JSON",
                        "/events": "the next line of the run's event log, as text (empty if none)",
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
