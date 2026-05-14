"""iowarp/clio-agent#16: session_export round-trip."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


@dataclass
class _Pred:
    answer: str = "ok"
    selected_expert: str = ""
    routing_rationale: str = ""


class _Agent:
    def forward(self, *args, **kwargs):
        return _Pred()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        build_app(sessions_path=tmp_path / "s.json", agent=_Agent())
    )


def test_export_unknown_session_404s(tmp_path: Path) -> None:
    c = _client(tmp_path)
    resp = c.get("/v1/sessions/sess_nope/export")
    assert resp.status_code == 404


def test_export_then_import_round_trip(tmp_path: Path) -> None:
    from .conftest import complete_turn

    c = _client(tmp_path)
    sid = c.post("/v1/sessions", json={"title": "src"}).json()["id"]
    complete_turn(c, sid, "first")
    complete_turn(c, sid, "second")

    blob = c.get(f"/v1/sessions/{sid}/export").json()
    assert blob["version"] == "1"
    assert blob["session"]["id"] == sid
    assert blob["session"]["title"] == "src"
    assert len(blob["messages"]) == 4  # 2 turns × (user + assistant)
    assert blob["workspace"]["id"] == "ws_default"

    # Re-import.
    new_sess = c.post("/v1/sessions/import", json=blob).json()
    assert new_sess["id"] != sid
    assert new_sess["title"] == "src"
    assert new_sess["message_count"] == 4

    rows = c.get(f"/v1/sessions/{new_sess['id']}/messages").json()["messages"]
    assert len(rows) == 4
    # Original user prompts preserved.
    user_texts = {
        p["text"] for m in rows
        for p in m["parts"]
        if m["role"] == "user" and p["type"] == "text"
    }
    assert {"first", "second"} == user_texts


def test_capabilities_advertises_session_export(tmp_path: Path) -> None:
    c = _client(tmp_path)
    body = c.get("/v1/capabilities").json()
    assert body["capabilities"]["session_export"] is True
