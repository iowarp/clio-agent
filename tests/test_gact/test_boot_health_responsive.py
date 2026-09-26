"""The server answers ``/v1/health`` within ~1 s of binding, whatever boot work is doing.

Regression for ares (2026-09-25): the deferred agent build constructed ARC -- the
clio-core connect-or-spawn + native attach -- inline on the event loop. A native
client waiting 30 s for its runtime (twice) meant uvicorn had bound 17800 but served
zero ``GET /v1/health`` requests, and ``clio start`` gave up after ~90 s. Also pins
the boot provider decision: the committed ``lm_studio`` default is not a user
selection, so an untouched headless server never probes LM Studio and reports
``lm_provider`` as ``unconfigured``.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from clio_agent.arc import clio_core_attach, clio_core_file_capacity, storage
from clio_agent.gact.app import build_app
from clio_agent.gact.providers.boot_selection import explicit_lm_provider
from tests._config_layer import delete_config, set_config

# The native client's WaitForLocalServer default: the stall this test simulates.
_FAKE_CLIO_CORE_WAIT_S = 30.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get_health(port: int, timeout: float) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", "/v1/health")
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def _row(body: dict[str, Any], name: str) -> dict[str, Any]:
    return {row["name"]: row for row in body["integrations"]}[name]


@pytest.fixture
def slow_clio_core(monkeypatch, tmp_path):
    """Make the clio-core attach block like a native client waiting on its runtime."""
    released = threading.Event()
    entered = threading.Event()

    class _SlowClioCoreStore(storage.LocalFSStore):
        def __init__(self, *, config_path: str) -> None:
            entered.set()
            released.wait(_FAKE_CLIO_CORE_WAIT_S)
            super().__init__(tmp_path / "attached-arc")

    cfg = tmp_path / "cte.yaml"
    cfg.write_text("networking:\n  port: 21045\n", encoding="utf-8")
    monkeypatch.setattr(storage, "ClioCoreStore", _SlowClioCoreStore)
    monkeypatch.setattr(clio_core_file_capacity, "preflight_clio_core_config", lambda *a, **k: None)
    set_config("arc.store", "cte")
    set_config("arc.store_config", str(cfg))
    clio_core_attach.reset_attach_state()
    yield entered, released
    released.set()
    clio_core_attach.reset_attach_state()


def test_health_answers_within_a_second_while_clio_core_attach_stalls(
    slow_clio_core, monkeypatch, tmp_path: Path
) -> None:
    entered, released = slow_clio_core
    monkeypatch.chdir(tmp_path)  # _process_arc's cwd-relative data dir
    # An LM Studio that is not there: a user-selected lm_studio with nothing listening.
    monkeypatch.setenv("CLIO_LM_PROVIDER", "lm_studio")
    monkeypatch.setenv("CLIO_LM_API_BASE", f"http://127.0.0.1:{_free_port()}/v1")
    delete_config("lm.model")

    app = build_app(sessions_path=tmp_path / "s.json", arc=None)  # the server builds its own
    app.state.server_boot = True
    app.state.want_agent = True
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30.0
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "uvicorn never bound"
        assert entered.wait(10.0), "ARC construction never started"

        began = time.monotonic()
        status, body = _get_health(port, timeout=_FAKE_CLIO_CORE_WAIT_S)
        elapsed = time.monotonic() - began

        # ~1 s on a Linux host (ares/homelab live runs); the bound here leaves headroom for
        # a loaded Windows CI box, where the boot reaper's psutil walk contends for the GIL.
        # The regression it guards is a loop blocked for the whole 30 s native wait.
        assert elapsed < 3.0, f"/v1/health took {elapsed:.2f}s while clio-core was attaching"
        assert status in (200, 503)
        assert _row(body, "doctor")["reason"] == "doctor_boot_collection_in_progress"
        attach = _row(body, "clio_core_attach")
        assert attach["reason"] == "clio_core_starting"
        assert attach["required"] is False

        released.set()  # the attach completes -> the typed state flips to attached
        attach_deadline = time.monotonic() + 20.0
        reason = attach["reason"]
        while reason != "clio_core_attached" and time.monotonic() < attach_deadline:
            time.sleep(0.1)
            reason = _row(_get_health(port, timeout=_FAKE_CLIO_CORE_WAIT_S)[1], "clio_core_attach")[
                "reason"
            ]
        assert reason == "clio_core_attached"
        # Once the boot doctor collection lands, health carries the full rows again.
        rows: dict[str, Any] = {}
        while time.monotonic() < attach_deadline:
            rows = {
                r["name"]: r
                for r in _get_health(port, timeout=_FAKE_CLIO_CORE_WAIT_S)[1]["integrations"]
            }
            if "gateway" in rows:
                break
            time.sleep(0.2)
        assert "gateway" in rows and "doctor" not in rows
    finally:
        released.set()
        server.should_exit = True
        thread.join(timeout=60.0)
    assert not thread.is_alive(), "server did not shut down"


def test_committed_lm_studio_default_is_not_a_provider_selection(monkeypatch) -> None:
    monkeypatch.delenv("CLIO_LM_PROVIDER", raising=False)
    delete_config("lm.provider")
    assert explicit_lm_provider() == ""  # the lm_studio base-layer default does not count

    monkeypatch.setenv("CLIO_LM_PROVIDER", "codex")
    assert explicit_lm_provider() == "codex"

    set_config("lm.provider", "claude_code")  # file layer wins, as everywhere in conf
    assert explicit_lm_provider() == "claude_code"


def test_unselected_provider_reports_unconfigured_not_an_outage(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CLIO_LM_PROVIDER", raising=False)
    monkeypatch.delenv("CLIO_DESKTOP_BOOT_HEARTBEAT", raising=False)
    delete_config("lm.provider")

    body = TestClient(build_app(sessions_path=tmp_path / "s.json")).get("/v1/health").json()

    lm = _row(body, "lm_provider")
    assert lm["reason"] == "lm_provider_unconfigured"
    assert lm["status"] == "ready"
    assert lm["required"] is False
