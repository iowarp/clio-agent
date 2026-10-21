"""iowarp/clio-agent#21 + #22 + #23: scheduled turns, sharing, extract."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
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
def client(tmp_path: Path) -> Iterator[TestClient]:
    with TestClient(build_app(sessions_path=tmp_path / "s.json")) as active:
        yield active


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


def test_schedule_delete_withdraws_pending_input_without_acknowledging_it(
    client: TestClient,
) -> None:
    from clio_agent.arc.task_types import MailItem
    from clio_agent.gact.session_input import accept_session_input

    app = cast(FastAPI, client.app)
    sid = client.post("/v1/sessions", json={"title": "held schedule"}).json()["id"]
    created = client.post(
        f"/v1/sessions/{sid}/schedules",
        json={"cron": "0 9 * * *", "question": "Inspect the pending work"},
    )
    assert created.status_code == 200, created.text
    schedule = created.json()

    def stage() -> MailItem:
        app.state.sessions.update(sid, metadata_patch={"execution_gate": "held-for-assertions"})
        identity = accept_session_input(
            app,
            sid,
            schedule["question"],
            metadata={"scheduled": True, "schedule_id": schedule["id"], "schedule_one_shot": False},
            steer_message_id=f"schedule:{schedule['id']}:0",
        )
        item = app.state.task_history.mail_item(sid, identity)
        assert item is not None
        return item

    assert client.portal is not None
    item = client.portal.call(app.state.task_history.io.run, stage)
    removed = client.delete(f"/v1/schedules/{schedule['id']}")
    assert removed.status_code == 204, removed.text
    assert app.state.schedules.get(schedule["id"]) is None
    assert app.state.task_history.mail_withdrawn(sid, item.id)
    assert app.state.task_history.mail_item(sid, item.id) == item
    assert app.state.task_history.pending_mail(sid) == ()
    assert not app.state.task_history.acknowledged_notifications(sid)


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

    state = cast(FastAPI, client.app).state
    state.shared_tokens[share["token"]]["expires_at"] = time.time() - 10
    resp = client.get(f"/v1/shared/{share['token']}")
    assert resp.status_code == 410


def test_capabilities_advertised(client: TestClient) -> None:
    body = client.get("/v1/capabilities").json()
    for flag in ("scheduled_sessions", "session_sharing"):
        assert body["capabilities"][flag] is True
    # Extraction wrote a flattened agent; agents are composed from plugins now.
    assert body["capabilities"]["skills_extraction"] is False
