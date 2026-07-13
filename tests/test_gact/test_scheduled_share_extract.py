"""iowarp/clio-agent#21 + #22 + #23: scheduled turns, sharing, extract."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.scheduler import cron_matches

# ---- #21: scheduler -------------------------------------------------------


def test_cron_matches_basic_fields() -> None:
    when = datetime(2026, 4, 25, 9, 30, tzinfo=timezone.utc)
    assert cron_matches("30 9 25 4 *", when)
    assert cron_matches("* * * * *", when)
    assert not cron_matches("0 9 25 4 *", when)


def test_cron_matches_step() -> None:
    # */15 minute fields fire at 0, 15, 30, 45.
    for minute in (0, 15, 30, 45):
        when = datetime(2026, 4, 25, 12, minute, tzinfo=timezone.utc)
        assert cron_matches("*/15 * * * *", when)
    when = datetime(2026, 4, 25, 12, 7, tzinfo=timezone.utc)
    assert not cron_matches("*/15 * * * *", when)


def test_cron_matches_comma_list() -> None:
    when = datetime(2026, 4, 25, 12, 5, tzinfo=timezone.utc)
    assert cron_matches("0,5,10 * * * *", when)
    assert not cron_matches("1,2,3 * * * *", when)


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json"))


def test_schedules_crud(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    new = client.post(
        f"/v1/sessions/{sid}/schedules",
        json={"cron": "0 9 * * *", "question": "morning summary"},
    ).json()
    assert new["session_id"] == sid
    assert new["cron"] == "0 9 * * *"
    assert new["question"] == "morning summary"
    rows = client.get(f"/v1/sessions/{sid}/schedules").json()["schedules"]
    assert len(rows) == 1
    assert client.delete(f"/v1/schedules/{new['id']}").status_code == 204
    assert client.get(f"/v1/sessions/{sid}/schedules").json()["schedules"] == []


def test_schedule_create_requires_fields(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    resp = client.post(f"/v1/sessions/{sid}/schedules", json={"cron": "* * * * *"})
    assert resp.status_code == 422


def test_schedules_unknown_session_404s(client: TestClient) -> None:
    resp = client.get("/v1/sessions/sess_nope/schedules")
    assert resp.status_code == 404


# ---- #22: sharing --------------------------------------------------------


def test_share_then_fetch(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    share = client.post(f"/v1/sessions/{sid}/share", json={}).json()
    assert share["session_id"] == sid
    assert share["token"].startswith("shr_")

    body = client.get(f"/v1/shared/{share['token']}").json()
    assert body["session"]["id"] == sid


def test_share_unknown_token_404s(client: TestClient) -> None:
    assert client.get("/v1/shared/shr_nope").status_code == 404


def test_share_expiry(client: TestClient) -> None:
    """ttl_s=1 returns a token that 410s after we trick the clock."""

    sid = client.post("/v1/sessions", json={"title": "t"}).json()["id"]
    share = client.post(f"/v1/sessions/{sid}/share", json={"ttl_s": 1}).json()
    # Force expiry by overwriting the row's expires_at.
    import time

    state = client.app.state
    state.shared_tokens[share["token"]]["expires_at"] = time.time() - 10
    resp = client.get(f"/v1/shared/{share['token']}")
    assert resp.status_code == 410


# ---- #23: skills_extraction ----------------------------------------------


def test_extract_agent_from_sessions(client: TestClient) -> None:
    """Seed two sessions with tools_called metadata; extract pulls
    the most-common tool names into a new agent."""

    sid1 = client.post("/v1/sessions", json={"title": "s1"}).json()["id"]
    sid2 = client.post("/v1/sessions", json={"title": "s2"}).json()["id"]

    # Inject messages directly via the in-memory log so we don't
    # need to drive a fake agent through POST /messages here.
    from clio_agent.gact.types import Message, Part

    state = client.app.state
    for sid, tools in [
        (sid1, ["hdf5_list_datasets", "hdf5_analyze_file"]),
        (sid2, ["hdf5_list_datasets", "parquet_analyze_schema"]),
    ]:
        state.messages.setdefault(sid, []).append(
            Message(
                id="msg_user_x",
                session_id=sid,
                role="user",
                created_at="2026-04-25T00:00:00Z",
                updated_at="2026-04-25T00:00:00Z",
                parts=[Part(id="part_x", type="text", text=f"analyze {sid}")],
            )
        )
        state.messages[sid].append(
            Message(
                id=f"msg_asst_{sid}",
                session_id=sid,
                role="assistant",
                created_at="2026-04-25T00:00:00Z",
                updated_at="2026-04-25T00:00:00Z",
                parts=[Part(id="part_y", type="text", text="done")],
                metadata={"tools_called": [{"name": t} for t in tools]},
            )
        )

    resp = client.post(
        "/v1/agents/extract",
        json={"session_ids": [sid1, sid2], "agent_id": "extracted_one"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == "extracted_one"
    assert body["source"] == "user"
    # hdf5_list_datasets appears twice -> top of the list.
    assert body["tools"][0] == "hdf5_list_datasets"


def test_extract_refuses_builtin_id(client: TestClient) -> None:
    sid = client.post("/v1/sessions", json={"title": "s"}).json()["id"]
    resp = client.post(
        "/v1/agents/extract",
        json={"session_ids": [sid], "agent_id": "data"},
    )
    assert resp.status_code == 409


def test_extract_requires_inputs(client: TestClient) -> None:
    resp = client.post("/v1/agents/extract", json={})
    assert resp.status_code == 422


def test_capabilities_advertised(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    for flag in ("scheduled_sessions", "session_sharing", "skills_extraction"):
        assert body["capabilities"][flag] is True
