"""LM Studio reuse/load waits on LM Studio, never a flat bound or a silent reload (#1577 4).

Driven against a REAL local HTTP server speaking LM Studio's ``/api/v1/models`` and
``/api/v1/models/load`` shapes, with the module's bounds shortened.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from clio_agent.gact import lm_studio_load

MODEL = "qwen-test"
LOADED = {
    "models": [
        {
            "key": MODEL,
            "loaded_instances": [
                {"id": MODEL, "config": {"context_length": 8192, "parallel": 1}},
            ],
        }
    ]
}


class _LMStudio:
    """Behaviour knobs for the fake server (seconds / status / payload)."""

    def __init__(self) -> None:
        self.list_delay = 0.0
        self.list_status = 200
        self.list_body: Any = LOADED
        self.load_delay = 0.0
        self.loads = 0
        self.silent = threading.Event()  # set -> every request hangs (LM Studio wedged)


@pytest.fixture
def lm_studio() -> Iterator[tuple[_LMStudio, str]]:
    state = _LMStudio()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return None

        def _reply(self, status: int, body: Any) -> None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if state.silent.is_set():
                time.sleep(30)
                return
            time.sleep(state.list_delay)
            self._reply(state.list_status, state.list_body)

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            state.loads += 1
            time.sleep(state.load_delay)
            if state.silent.is_set():
                time.sleep(30)
                return
            self._reply(200, {"instance_id": MODEL, "status": "loaded"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield state, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _match(root: str) -> str:
    return lm_studio_load.loaded_instance_matching(
        root, {}, model=MODEL, context_length=8192, parallel=1
    )


def test_a_slow_listing_is_retried_longer_and_reuses_the_loaded_model(
    lm_studio: tuple[_LMStudio, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The listing answers after 1 s; the first bound is 0.3 s: retried, model reused.

    SABOTAGE: one bound only (``LIST_TIMEOUTS_S[:1]``-style, the old swallowed 10 s) ->
    the timeout reads as "not loaded" / fails -> red.
    """
    state, root = lm_studio
    state.list_delay = 1.0
    monkeypatch.setattr(lm_studio_load, "LIST_TIMEOUTS_S", (0.3, 3.0))
    assert _match(root) == MODEL


def test_a_listing_that_never_answers_fails_typed_instead_of_reloading(
    lm_studio: tuple[_LMStudio, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """SABOTAGE: return "" on timeout (the old swallow) -> no error -> red."""
    state, root = lm_studio
    state.list_delay = 5.0
    monkeypatch.setattr(lm_studio_load, "LIST_TIMEOUTS_S", (0.2, 0.3))
    with pytest.raises(lm_studio_load.LMStudioLoadError) as caught:
        _match(root)
    assert caught.value.reason == lm_studio_load.REASON_LIST_UNANSWERED
    assert state.loads == 0


def test_a_refused_listing_is_logged_typed_before_loading(
    lm_studio: tuple[_LMStudio, str], caplog: pytest.LogCaptureFixture
) -> None:
    """SABOTAGE: drop the warning on a >= 400 listing -> no typed record -> red."""
    state, root = lm_studio
    state.list_status = 404
    with caplog.at_level(logging.WARNING, logger=lm_studio_load.__name__):
        assert _match(root) == ""
    assert any(
        f"reason={lm_studio_load.REASON_LIST_REJECTED}" in r.getMessage() for r in caplog.records
    )


def test_a_load_is_waited_for_while_lm_studio_answers(
    lm_studio: tuple[_LMStudio, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2 s load with a 0.5 s unresponsive window succeeds: LM Studio keeps answering.

    SABOTAGE: bound the load POST by ``LOAD_UNRESPONSIVE_S`` (a flat cut) -> red.
    """
    state, root = lm_studio
    state.load_delay = 2.0
    monkeypatch.setattr(lm_studio_load, "LOAD_POLL_S", 0.1)
    monkeypatch.setattr(lm_studio_load, "LOAD_PROBE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(lm_studio_load, "LOAD_UNRESPONSIVE_S", 0.5)
    response = lm_studio_load.load_model(root, {}, {"model": MODEL})
    assert response.status_code == 200
    assert response.json()["instance_id"] == MODEL


def test_a_load_whose_server_goes_silent_fails_typed(
    lm_studio: tuple[_LMStudio, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """LM Studio stops answering mid-load: typed failure after the silent window.

    SABOTAGE: treat a failed liveness probe as an answer -> the load waits out the
    30 s hang -> the elapsed assertion fails -> red.
    """
    state, root = lm_studio
    state.load_delay = 0.2
    state.silent.set()
    monkeypatch.setattr(lm_studio_load, "LOAD_POLL_S", 0.1)
    monkeypatch.setattr(lm_studio_load, "LOAD_PROBE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(lm_studio_load, "LOAD_UNRESPONSIVE_S", 0.6)
    started = time.monotonic()
    with pytest.raises(lm_studio_load.LMStudioLoadError) as caught:
        lm_studio_load.load_model(root, {}, {"model": MODEL})
    assert caught.value.reason == lm_studio_load.REASON_UNRESPONSIVE_DURING_LOAD
    assert time.monotonic() - started < 10
