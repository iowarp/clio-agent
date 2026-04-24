"""CLIO-BBBBBBBBBB24: tokens + cost_usd propagate through every layer.

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

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = "data_expert"
    routing_rationale: str = ""
    tokens: dict = None  # type: ignore[assignment]
    cost_usd: float = 0.0


class _Agent:
    def __init__(self, pred):
        self._pred = pred

    def forward(self, question: str, session_id: str):
        return self._pred


def _client(tmp_path: Path, pred) -> TestClient:
    return TestClient(
        build_app(sessions_path=tmp_path / "s.json", agent=_Agent(pred))
    )


def _turn(client: TestClient, sid: str) -> dict:
    return client.post(
        f"/v1/sessions/{sid}/messages",
        json={"parts": [{"type": "text", "text": "hello"}]},
    ).json()


def test_turn_with_cost_populates_every_surface(tmp_path: Path) -> None:
    pred = _Pred(
        tokens={"input": 100, "output": 50, "cache_read": 40, "cache_write": 0},
        cost_usd=0.0032,
    )
    client = _client(tmp_path, pred)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]

    resp = _turn(client, sid)
    a = resp["assistant_message"]
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
    client = _client(tmp_path, pred)
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    resp = _turn(client, sid)
    a = resp["assistant_message"]
    assert a["tokens"]["input"] == 0
    assert a["cost_usd"] == 0.0
