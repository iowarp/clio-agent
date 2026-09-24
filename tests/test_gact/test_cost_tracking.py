"""tokens + cost_usd propagate through every layer.

The Prediction can carry ``.tokens`` (dict or attr) and ``.cost_usd``;
the POST-message path threads those onto:

  - assistant_message.tokens / cost_usd / stop_reason
  - message.completed SSE payload
  - Session's cumulative tokens_input / tokens_output / cost_usd
  - /v1/metrics tokens + cost rollups

Turns with no cost data keep the envelope shape but report zeros.
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


V3_HEADERS = {"X-GACT-Version": "0.3"}


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""
    tokens: dict = None  # type: ignore[assignment]
    cost_usd: float = 0.0


@dataclass
class _PredNoCostSource:
    """A prediction that never carries ``.cost_usd`` at all -- distinct from
    ``_Pred(cost_usd=0.0)``, which explicitly reports a real zero. Simulates a
    local/self-hosted model the price table has never heard of (#775: no
    silent fallback -- the missing source must surface as unknown, not $0)."""

    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""
    tokens: dict = None  # type: ignore[assignment]


class _Agent:
    def __init__(self, pred):
        self._pred = pred

    def forward(self, question: str, session_id: str):
        return self._pred


def _client(tmp_path: Path, pred) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=_Agent(pred)))


def _turn(client: TestClient, sid: str) -> dict:
    from .conftest import complete_turn

    return complete_turn(client, sid, "hello")


def test_turn_with_cost_populates_every_surface(tmp_path: Path) -> None:
    pred = _Pred(
        tokens={"input": 100, "output": 50, "cache_read": 40, "cache_write": 0},
        cost_usd=0.0032,
    )
    with _client(tmp_path, pred) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

        a = _turn(client, sid)
        assert a["tokens"]["input"] == 100
        assert a["tokens"]["output"] == 50
        assert a["cost_usd"] == 0.0032
        assert a["stop_reason"] == "end_turn"

        # Session rollup.
        s = client.get(f"/v1/sessions/{sid}").json()
        assert s["tokens_input"] == 100
        assert s["tokens_output"] == 50
        assert abs(s["cost_usd"] - 0.0032) < 1e-9

        # Fire another turn to confirm cumulation.
        _turn(client, sid)
        s = client.get(f"/v1/sessions/{sid}").json()
        assert s["tokens_input"] == 200
        assert s["tokens_output"] == 100
        assert abs(s["cost_usd"] - 0.0064) < 1e-9

        # /v1/metrics reflects the sum.
        m = client.get("/v1/metrics").json()
        assert m["tokens"]["input_total"] == 200
        assert m["tokens"]["output_total"] == 100
        assert abs(m["cost"]["total_usd"] - 0.0064) < 1e-9


def test_turn_without_cost_keeps_zero_envelope(tmp_path: Path) -> None:
    pred = _Pred(tokens=None, cost_usd=0.0)
    with _client(tmp_path, pred) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        a = _turn(client, sid)
        assert a["tokens"]["input"] == 0
        assert a["cost_usd"] == 0.0


def test_v3_session_projection_serializes_known_cost_totals(tmp_path: Path) -> None:
    """iowarp/gact-tui U1: the v3 session row exposes the cumulative rollup
    the v0.2 row already carried internally, as a real (non-null) number once
    a turn actually reported one."""

    pred = _Pred(
        tokens={"input": 100, "output": 50, "cache_read": 0, "cache_write": 0},
        cost_usd=0.0032,
    )
    with _client(tmp_path, pred) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        assert "tokens_input" not in next(
            row
            for row in client.get("/v1/sessions", headers=V3_HEADERS).json()["sessions"]
            if row["id"] == sid
        )

        _turn(client, sid)

        row = next(
            row
            for row in client.get("/v1/sessions", headers=V3_HEADERS).json()["sessions"]
            if row["id"] == sid
        )
        assert row["tokens_input"] == 100
        assert row["tokens_output"] == 50
        assert row["cost_usd"] == pytest.approx(0.0032)


def test_v3_session_projection_reports_unknown_cost_as_null(tmp_path: Path) -> None:
    """A turn that counted real tokens but never got a cost from any source
    (no provider report, no price-table match) must serialize cost_usd as
    null -- never a fabricated $0.00 (#775 no silent fallback)."""

    pred = _PredNoCostSource(tokens={"input": 300, "output": 90, "cache_read": 0, "cache_write": 0})
    with _client(tmp_path, pred) as client:
        sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
        _turn(client, sid)

        row = next(
            row
            for row in client.get("/v1/sessions", headers=V3_HEADERS).json()["sessions"]
            if row["id"] == sid
        )
        assert row["tokens_input"] == 300
        assert row["tokens_output"] == 90
        assert row["cost_usd"] is None
