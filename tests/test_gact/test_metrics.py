"""tests for GET /v1/metrics.

The endpoint summarises runtime state — sessions by status + the
in-memory message counts — into the SPEC §6.16 wire envelope.
Token/cost/latency rollups stay zero until the optimizer layer
lands; the shape is the same so v0.2 clients (and conformance
tests) don't care.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app

# #948 S4b: default sessions run the blueprint react ``main``; route it to each
# test's ``build_app(agent=...)`` host fake.
pytestmark = pytest.mark.usefixtures("host_agent_executor")


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = "keyword match"


class _FakeAgent:
    def forward(self, question: str, session_id: str):
        return _Pred()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "sessions.json", agent=_FakeAgent()))


def test_metrics_empty_state(client: TestClient) -> None:
    """Before any POSTs the endpoint returns a zero-ish skeleton
    with a real uptime — NOT a 501."""

    resp = client.get("/v1/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["uptime_s"] >= 0
    assert body["sessions"]["total"] == 0
    assert body["sessions"]["active"] == 0
    assert body["messages"]["total"] == 0
    # Token/cost sections exist but are zeroed.
    for k in ("input_total", "output_total", "cache_read_total", "cache_write_total"):
        assert body["tokens"][k] == 0
    assert body["cost"]["total_usd"] == 0.0


def test_metrics_reflects_session_and_message_counts(client: TestClient) -> None:
    """After a session with one turn, totals line up."""

    sess = client.post("/v1/sessions", json={"title": "m"}).json()
    sid = sess["id"]
    resp = client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "analyze /tmp/f.hdf5"}]},
    )
    assert resp.status_code == 200

    body = client.get("/v1/metrics").json()
    assert body["sessions"]["total"] == 1
    assert body["messages"]["total"] == 2  # user + assistant
    assert body["messages"]["by_role"].get("user") == 1
    assert body["messages"]["by_role"].get("assistant") == 1
    # After the turn settles the session should be 'idle'.
    assert body["sessions"]["by_status"].get("idle", 0) >= 1


def test_metrics_latencies_aggregate_tool_call_durations(client: TestClient) -> None:
    # iowarp/clio-agent#655: /v1/metrics.latencies must report real recorded
    # tool-call durations (per tool + an overall tool_call bucket), not {}.
    from clio_agent.gact.app import _append_session_message
    from clio_agent.gact.types import Message

    app = client.app
    sid = "sess_metrics"
    # #770 C3: write through the real session_store seam (production never
    # mutates app.state.messages directly), so the running metrics_counters
    # aggregate that /v1/metrics reads is kept in lock-step.
    _append_session_message(
        app,
        sid,
        Message(
            id="m1",
            session_id=sid,
            role="assistant",
            created_at="t",
            updated_at="t",
            metadata={
                "tools_called": [
                    {"name": "fs_read_file", "ok": True, "duration_ms": 10.0},
                    {"name": "fs_read_file", "ok": True, "duration_ms": 30.0},
                    {"name": "hdf5.analyze", "ok": True, "duration_ms": 100.0},
                    {"name": "noisy", "ok": True, "duration_ms": 0},  # dropped (<=0)
                ]
            },
        ),
    )
    lat = client.get("/v1/metrics").json()["latencies"]
    assert lat["tool_call"]["count"] == 3
    assert lat["tool_call"]["max_ms"] == 100.0
    assert lat["tool:fs_read_file"]["count"] == 2
    assert "tool:noisy" not in lat  # zero/invalid durations excluded
