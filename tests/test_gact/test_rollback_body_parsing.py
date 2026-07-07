"""Undo/rewind request-body parsing routes through ``json_body`` (#772).

The undo/rewind handlers historically did ``try: body = await request.json()
except json.JSONDecodeError: body = {}`` -- a malformed body was swallowed to an
empty mapping *silently*. Routing them through the shared ``json_body`` helper
preserves that behavior (a malformed/absent body still resolves to ``{}`` and the
handler proceeds on defaults) while emitting a structured
``request_body_unparseable`` reason so the degraded parse is visible in the trace
and assertable via ``caplog`` -- matching the other 13 route modules already on
``json_body``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.routes._body import REQUEST_BODY_UNPARSEABLE_REASON
from clio_agent.gact.types import Message, Part

_BODY_LOGGER = "clio_agent.gact.routes.body"
_MALFORMED = b"{not valid json"
_HEADERS = {"content-type": "application/json"}


def _seed(client: TestClient, sid: str, message_ids: list[str]) -> None:
    messages = [
        Message(
            id=mid,
            session_id=sid,
            role="assistant",
            created_at="2026-05-20T00:00:00+00:00",
            updated_at="2026-05-20T00:00:00+00:00",
            parts=[Part(id=f"part_{mid}", type="text", text=mid)],
        )
        for mid in message_ids
    ]
    client.app.state.messages[sid] = messages
    client.app.state.sessions.update(sid, message_count=len(messages))


def _unparseable_records(caplog: pytest.LogCaptureFixture, route: str) -> list[logging.LogRecord]:
    return [
        rec
        for rec in caplog.records
        if getattr(rec, "structured_reason", {}).get("reason")
        == REQUEST_BODY_UNPARSEABLE_REASON["reason"]
        and getattr(rec, "structured_reason", {}).get("route") == route
    ]


def test_undo_malformed_body_defaults_to_empty_and_logs_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed undo body still resolves to ``{}`` (count defaults to 1, one
    message removed) AND emits a structured request_body_unparseable warning."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "undo"}).json()["id"]
        _seed(client, sid, ["msg_1", "msg_2", "msg_3"])
        with caplog.at_level(logging.WARNING, logger=_BODY_LOGGER):
            resp = client.post(f"/v1/sessions/{sid}/undo", content=_MALFORMED, headers=_HEADERS)

    assert resp.status_code == 200, resp.text
    # Behavior identical to an empty-object body: default count == 1.
    assert resp.json()["deleted_message_ids"] == ["msg_3"]
    records = _unparseable_records(caplog, "POST /v1/sessions/{sid}/undo")
    assert records, "expected a request_body_unparseable warning for the malformed undo body"


def test_rewind_malformed_body_defaults_to_empty_and_logs_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed rewind body resolves to ``{}`` (empty target -> 422 "rewind
    requires message_id", unchanged) AND emits a structured
    request_body_unparseable warning before that guard is reached."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rewind"}).json()["id"]
        _seed(client, sid, ["msg_1", "msg_2"])
        with caplog.at_level(logging.WARNING, logger=_BODY_LOGGER):
            resp = client.post(f"/v1/sessions/{sid}/rewind", content=_MALFORMED, headers=_HEADERS)

    assert resp.status_code == 422, resp.text
    records = _unparseable_records(caplog, "POST /v1/sessions/{sid}/rewind")
    assert records, "expected a request_body_unparseable warning for the malformed rewind body"


def test_undo_non_object_body_is_rejected_and_deletes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A valid-JSON *non-object* undo body (e.g. ``[1, 2]``) must be rejected
    with the pre-#772 422 validation_error ("undo request body must be an
    object") and must NOT delete any message.

    Regression test for the d8750ee behavior change where ``json_body`` coerced
    a parsed list/scalar to ``{}`` and the destructive undo proceeded (200,
    message deleted) instead of 422.
    """

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "undo-non-object"}).json()["id"]
        _seed(client, sid, ["msg_1", "msg_2", "msg_3"])
        with caplog.at_level(logging.WARNING, logger=_BODY_LOGGER):
            resp = client.post(f"/v1/sessions/{sid}/undo", json=[1, 2])
        surviving = [m.id for m in client.app.state.messages[sid]]

    assert resp.status_code == 422, resp.text
    info = resp.json()["error"]
    assert info["error"] == "validation_error"
    assert info["message"] == "undo request body must be an object"
    assert info["recoverable"] is True
    assert surviving == ["msg_1", "msg_2", "msg_3"], "422 must not delete messages"
    # An explicit 422 rejection is not a silent fallback -- no degraded-parse
    # reason should be recorded for it.
    assert not _unparseable_records(caplog, "POST /v1/sessions/{sid}/undo")


def test_rewind_non_object_body_is_rejected_with_object_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A valid-JSON non-object rewind body must keep the pre-#772 422 message
    ("rewind request body must be an object"), not the later "rewind requires
    message_id" guard, and must not delete anything."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rewind-non-object"}).json()["id"]
        _seed(client, sid, ["msg_1", "msg_2"])
        with caplog.at_level(logging.WARNING, logger=_BODY_LOGGER):
            resp = client.post(f"/v1/sessions/{sid}/rewind", json=["msg_1"])
        surviving = [m.id for m in client.app.state.messages[sid]]

    assert resp.status_code == 422, resp.text
    info = resp.json()["error"]
    assert info["error"] == "validation_error"
    assert info["message"] == "rewind request body must be an object"
    assert surviving == ["msg_1", "msg_2"], "422 must not delete messages"
    assert not _unparseable_records(caplog, "POST /v1/sessions/{sid}/rewind")


def test_rewind_null_body_is_rejected_with_object_message(tmp_path: Path) -> None:
    """A JSON ``null`` rewind body hit the same pre-#772 non-object 422 guard
    (rewind had no ``body is None`` coercion, unlike undo)."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "rewind-null"}).json()["id"]
        _seed(client, sid, ["msg_1", "msg_2"])
        resp = client.post(f"/v1/sessions/{sid}/rewind", content=b"null", headers=_HEADERS)

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["message"] == "rewind request body must be an object"


def test_undo_null_body_defaults_to_empty_and_logs_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A JSON ``null`` undo body keeps the pre-#772 coercion to ``{}`` (undo
    explicitly mapped ``None`` to an empty body: 200, default count of 1) while
    now emitting the structured degraded-parse reason instead of doing it
    silently."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "undo-null"}).json()["id"]
        _seed(client, sid, ["msg_1", "msg_2", "msg_3"])
        with caplog.at_level(logging.WARNING, logger=_BODY_LOGGER):
            resp = client.post(f"/v1/sessions/{sid}/undo", content=b"null", headers=_HEADERS)

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_message_ids"] == ["msg_3"]
    assert _unparseable_records(caplog, "POST /v1/sessions/{sid}/undo")


def test_undo_wellformed_empty_body_does_not_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A well-formed ``{}`` body is the baseline: same result, NO warning (pins
    that the reason fires only on an actual parse failure, not every call)."""

    app = build_app(sessions_path=tmp_path / "s.json")
    with TestClient(app) as client:
        sid = client.post("/v1/sessions", json={"title": "undo-clean"}).json()["id"]
        _seed(client, sid, ["msg_1", "msg_2", "msg_3"])
        with caplog.at_level(logging.WARNING, logger=_BODY_LOGGER):
            resp = client.post(f"/v1/sessions/{sid}/undo", json={})

    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted_message_ids"] == ["msg_3"]
    assert not _unparseable_records(caplog, "POST /v1/sessions/{sid}/undo")
