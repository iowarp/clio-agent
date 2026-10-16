"""Local HTTP fixtures shared by model lifecycle tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest


@pytest.fixture
def endpoint() -> Iterator[tuple[str, dict[str, Any]]]:
    state: dict[str, Any] = {
        "open": 0,
        "max_open": 0,
        "active": 0,
        "max_active": 0,
        "calls": 0,
        "connections": 0,
        "requests": [],
        "hold": False,
        "started": threading.Event(),
    }
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            with lock:
                state["open"] += 1
                state["connections"] += 1
                state["max_open"] = max(state["max_open"], state["open"])

        def finish(self) -> None:
            try:
                super().finish()
            finally:
                with lock:
                    state["open"] -= 1

        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                state["calls"] += 1
                state["requests"].append((request["model"], self.headers.get("Authorization")))
            state["started"].set()
            try:
                text = (
                    "[[ ## progress ## ]]\nStep recorded.\n\n[[ ## met ## ]]\nFalse\n\n"
                    "[[ ## reason ## ]]\nContinue test.\n\n[[ ## next_action ## ]]\ncontinue\n\n[[ ## completed ## ]]"
                )
                data = json.dumps(
                    {
                        "id": "one",
                        "object": "chat.completion",
                        "model": request["model"],
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if state["hold"]:
                    # Detect the real client's close while its response is pending.
                    self.connection.settimeout(3)
                    self.connection.recv(1)
                    return
                self.wfile.write(data)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                pass
            finally:
                with lock:
                    state["active"] -= 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
