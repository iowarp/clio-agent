"""Bounded local-browser receiver; token exchange stays in StorageAuth."""

from __future__ import annotations

import hmac
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CALLBACK_PATH = "/clio-storage-return"


class _ReturnHandler(BaseHTTPRequestHandler):
    server: _ReturnServer

    def setup(self) -> None:
        """Bound incomplete browser requests as well as the overall authorization flow."""
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, format: str, *args: object) -> None:
        """Never log callback URLs, authorization codes, or state."""

    def do_GET(self) -> None:
        """Receive one state-validated callback without exposing it in the page."""
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        owner = self.server.owner
        state = query.get("state", [""])
        valid = (
            len(self.path) <= 16384
            and parsed.path == CALLBACK_PATH
            and not parsed.scheme
            and not parsed.netloc
            and len(state) == 1
            and hmac.compare_digest(state[0].encode(), owner.state.encode())
            and len(query.get("code", [])) <= 1
            and len(query.get("error", [])) <= 1
            and bool(query.get("code", [""])[0] or query.get("error", [""])[0])
        )
        if valid:
            with owner.lock:
                valid = not owner.stopped.is_set()
                if valid:
                    owner.callback = owner.redirect_uri + "?" + parsed.query
                    owner.stopped.set()
        message = (
            "Authorization received. Return to CLIO to finish signing in."
            if valid and not query.get("error")
            else "Sign-in was not completed. Return to CLIO and try again."
        )
        body = (
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>CLIO sign-in</title><body><main><h1>Return to CLIO</h1>"
            f"<p>{message}</p></main></body></html>"
        ).encode()
        self.send_response(200 if valid else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)


class _ReturnServer(ThreadingHTTPServer):
    def __init__(self, owner: StorageOAuthReturn) -> None:
        self.owner = owner
        super().__init__(("127.0.0.1", 0), _ReturnHandler)
        self.timeout = 0.2


class StorageOAuthReturn:
    """Listen before opening Google, then close on completion, cancellation or expiry."""

    def __init__(self, state: str, expires_at: float) -> None:
        self.state = state
        self.expires_at = expires_at
        self.callback = ""
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.server = _ReturnServer(self)
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_port}{CALLBACK_PATH}"
        self.thread = threading.Thread(target=self._serve, name="storage-oauth-return", daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            while not self.stopped.is_set() and time.time() < self.expires_at:
                self.server.handle_request()
        finally:
            self.server.server_close()
            self.stopped.set()

    def result(self) -> str:
        """Return a callback only to the initiating authenticated CLIO flow."""
        with self.lock:
            return self.callback

    def close(self) -> None:
        """Release the listener and discard its in-memory authorization response."""
        self.stopped.set()
        self.thread.join(timeout=1)
        with self.lock:
            self.callback = ""
