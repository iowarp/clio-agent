"""Serve a private loopback return page for manual storage sign-in qualification.

Run on the computer holding the browser, even when CLIO runs on an SSH node.
This helper never contacts CLIO, exchanges codes, or logs callback URLs.
"""

from __future__ import annotations

import argparse
import html
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

CALLBACK_PATH = "/clio-storage-return"


class ReturnHandler(BaseHTTPRequestHandler):
    """Display a callback only to its browser without retaining or transmitting it."""

    def log_message(self, format: str, *args: object) -> None:
        """Never print request paths: OAuth codes and state are confidential."""

    def do_GET(self) -> None:
        """Render the exact loopback callback for CLIO's trusted completion field."""
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path != CALLBACK_PATH or len(self.path) > 16384:
            self.send_error(404)
            return
        port = self.server.server_port
        callback = f"http://127.0.0.1:{port}{self.path}"
        authorized = bool(query.get("code", [""])[0] and query.get("state", [""])[0])
        text = (
            "<h1>Return to CLIO</h1><p>Copy this return URL into the storage sign-in "
            "dialog you opened. Keep it out of the conversation.</p>"
            f'<textarea aria-label="Sign-in return URL" readonly rows="7">{html.escape(callback)}</textarea>'
            if authorized
            else "<h1>Sign-in was not completed</h1><p>Return to CLIO and start sign-in again.</p>"
        )
        body = (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>CLIO storage sign-in</title><body><main>" + text + "</main></body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    """Listen on loopback for at most ten minutes, with no background daemon."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=48173)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Choose an unprivileged port between 1024 and 65535")
    with HTTPServer(("127.0.0.1", args.port), ReturnHandler) as server:
        server.timeout = 1
        print(f"CLIO return page: http://127.0.0.1:{args.port}{CALLBACK_PATH}", flush=True)
        print(
            "Open sign-in from CLIO. This helper stops in 10 minutes; Ctrl+C stops it earlier.",
            flush=True,
        )
        deadline = time.monotonic() + 600
        try:
            while time.monotonic() < deadline:
                server.handle_request()
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
