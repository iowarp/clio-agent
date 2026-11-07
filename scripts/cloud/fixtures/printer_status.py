"""A fixture 3D printer's status server for the live-status showcase (issue #1908).

``GET /status`` answers the printer's state as JSON: it heats, prints layer by layer
(progress, nozzle and bed temperatures, the layer, time left), then reports ``done``.
``GET /`` describes the endpoint. A print takes ``--print-s`` seconds after ``--heat-s``
seconds of heating, so a watcher sees the whole arc in a few minutes.

    python scripts/cloud/fixtures/printer_status.py --port 8799 --print-s 240
"""

from __future__ import annotations

import argparse
import json
import math
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

START = time.monotonic()


def status(heat_s: float, print_s: float, layers: int) -> dict[str, object]:
    elapsed = time.monotonic() - START
    if elapsed < heat_s:
        share = elapsed / heat_s
        return {
            "printer": "Prusa MK4 (bay 2)",
            "job": "bracket_v3.gcode",
            "state": "heating",
            "progress": 0.0,
            "layer": 0,
            "layers": layers,
            "nozzle_c": round(25 + share * 190, 1),
            "nozzle_target_c": 215,
            "bed_c": round(24 + share * 36, 1),
            "bed_target_c": 60,
            "seconds_left": round(heat_s - elapsed + print_s),
        }
    done = min(1.0, (elapsed - heat_s) / print_s)
    wobble = math.sin(elapsed / 3.0)
    return {
        "printer": "Prusa MK4 (bay 2)",
        "job": "bracket_v3.gcode",
        "state": "done" if done >= 1.0 else "printing",
        "progress": round(done * 100, 1),
        "layer": min(layers, int(done * layers)),
        "layers": layers,
        "nozzle_c": round(215 + 1.5 * wobble, 1) if done < 1.0 else 40.0,
        "nozzle_target_c": 215 if done < 1.0 else 0,
        "bed_c": round(60 + 0.4 * wobble, 1) if done < 1.0 else 35.0,
        "bed_target_c": 60 if done < 1.0 else 0,
        "seconds_left": max(0, round((1.0 - done) * print_s)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--heat-s", type=float, default=20.0)
    parser.add_argument("--print-s", type=float, default=240.0)
    parser.add_argument("--layers", type=int, default=120)
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the http.server interface
            if self.path.split("?")[0] == "/status":
                body = status(args.heat_s, args.print_s, args.layers)
            else:
                body = {"endpoints": {"/status": "the printer's current state as JSON"}}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args: object) -> None:
            return

    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
