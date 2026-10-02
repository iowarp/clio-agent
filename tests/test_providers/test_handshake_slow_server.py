"""A slow provider server is reported slow, never as down (#1577 3.10).

The handshake's 4/8/8 s HTTP bounds used to turn a slow server into the same
``UNREACHABLE`` verdict as one that is not running. A timed-out handshake is now retried
once with longer bounds; a server that answers within them is simply OK, and one that
still does not is ``ConnectivityState.TIMEOUT`` ("slow or unresponsive"). Driven through
the REAL OpenAI-compatible handshake against a REAL loopback HTTP server that answers late
(the handshake's own bounds shortened so the test is fast).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from clio_agent.providers.catalog import get_provider
from clio_agent.providers.handshake import get_handshake_for
from clio_agent.providers.handshake.base import HandshakeContext
from clio_agent.providers.handshake.model import ConnectivityState
from clio_agent.providers.handshake.unreachable import SERVER_NOT_ANSWERING


@pytest.fixture
def slow_server() -> Iterator[tuple[dict[str, float], str]]:
    knobs = {"delay": 0.0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            time.sleep(knobs["delay"])
            if self.path.endswith("/models"):
                body = json.dumps({"object": "list", "data": [{"id": "m1"}]}).encode()
                self.send_response(200)
            else:
                body = b"{}"
                self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass  # the client gave up waiting (a timed-out attempt)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield knobs, f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def _handshake() -> Any:
    provider = get_provider("vllm")
    assert provider is not None
    handshake = get_handshake_for(provider.provider_kind, provider)
    # The bounds are the handshake's own tunables (subclasses override them too).
    handshake.timeout_connect = 0.3
    handshake.timeout_models = 0.3
    handshake.timeout_model_config = 0.3
    return provider, handshake


def _ctx(provider: Any, api_base: str) -> HandshakeContext:
    return HandshakeContext(
        provider_id="vllm",
        provider_kind=provider.provider_kind,
        api_base=api_base,
        api_key="local",
        allow_external_sources=False,
    )


async def test_a_server_slower_than_the_first_bound_is_found_on_the_retry(
    slow_server: tuple[dict[str, float], str],
) -> None:
    """Answers take 0.6 s; the first bound is 0.3 s: the retry (x4) finds the model.

    SABOTAGE: drop the retry in ``ProviderHandshake.handshake`` -> UNREACHABLE /
    server_not_answering -> red.
    """
    knobs, api_base = slow_server
    knobs["delay"] = 0.6
    provider, handshake = _handshake()
    report = await handshake.handshake(_ctx(provider, api_base))
    assert report.connectivity == ConnectivityState.OK, report.error
    assert [m.id for m in report.models] == ["m1"]


async def test_a_server_that_never_answers_in_time_is_reported_slow_not_down(
    slow_server: tuple[dict[str, float], str],
) -> None:
    """SABOTAGE: keep the UNREACHABLE verdict on the retried timeout -> red."""
    knobs, api_base = slow_server
    knobs["delay"] = 3.0
    provider, handshake = _handshake()
    report = await handshake.handshake(_ctx(provider, api_base))
    assert report.connectivity == ConnectivityState.TIMEOUT
    assert report.error_code == SERVER_NOT_ANSWERING
    assert "slow or unresponsive" in (report.error or "")
