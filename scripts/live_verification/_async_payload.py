"""Owned HTTP test data for timing actual production MCP fetches."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class PayloadServer:
    """Serve known bytes slowly and record actual client transport outcomes."""

    def __init__(self, proof: Path) -> None:
        self.payload = b"Owned asynchronous task qualification data.\n" * 256
        self.sha256 = hashlib.sha256(self.payload).hexdigest()
        (proof / "payload.txt").write_bytes(self.payload)
        self.events = proof / "payload-events.jsonl"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.record("request_started", path=self.path)
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(owner.payload)))
                self.end_headers()
                try:
                    for offset in range(0, len(owner.payload), 512):
                        self.wfile.write(owner.payload[offset : offset + 512])
                        self.wfile.flush()
                        time.sleep(0.8)
                except (BrokenPipeError, ConnectionResetError):
                    owner.record("client_disconnected")
                else:
                    owner.record("request_finished", bytes=len(owner.payload), sha256=owner.sha256)

            def log_message(self, format: str, *args: Any) -> None:
                pass  # HTTP outcomes are recorded in the structured evidence ledger.

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/owned-task-payload.txt"

    def record(self, event: str, **details: Any) -> None:
        """Append a timestamped byte/outcome receipt without credentials."""
        with self.events.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": event, "time_ns": time.time_ns(), **details}) + "\n")

    def close(self) -> None:
        """Stop this harness-owned server after its test turn ends."""
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
