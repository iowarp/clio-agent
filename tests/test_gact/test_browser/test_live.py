"""The shared tab: frames and state to viewers, the person's input back, view-only viewers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.timeout(120)  # a real Chromium streams frames


def _client(tmp_path: Path) -> tuple[TestClient, Any, str]:
    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json", arc=None)
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "Shared"}).json()["id"]
    return client, app, sid


def _until_state(socket: Any, predicate: Any, limit: int = 200) -> dict[str, Any]:
    for _ in range(limit):
        message = socket.receive()
        if message.get("text"):
            state = json.loads(message["text"])
            if predicate(state):
                return state
    raise AssertionError("no matching state message")


def _frame_and_viewport(socket: Any, first: dict[str, Any]) -> tuple[bytes, dict[str, int]]:
    """Read until a frame arrived and some state named the frame's viewport."""
    frame, viewport = b"", first.get("viewport") or {}
    for _ in range(200):
        if frame and viewport.get("width"):
            return frame, viewport
        message = socket.receive()
        if message.get("bytes"):
            frame = message["bytes"]
        elif message.get("text"):
            viewport = json.loads(message["text"]).get("viewport") or viewport
    raise AssertionError("no frame or viewport")


def test_a_viewer_sees_frames_and_its_click_reaches_the_page(
    browser: Any, site: dict[str, Any], tmp_path: Path
) -> None:
    client, _app, sid = _client(tmp_path)
    record = browser.wait(browser.open(sid, "Shared"))
    browser.run(record, "goto", lambda r: r.page.goto(site["a"] + "/frames"))
    pages = client.get(f"/v1/sessions/{sid}/browser/pages").json()["pages"]
    assert [p["id"] for p in pages] == [record.id] and pages[0]["host"] == "a.fixture.localhost"
    url = f"/v1/sessions/{sid}/browser/{record.id}/live"
    with client.websocket_connect(url, subprotocols=["clio.browser.v1"]) as socket:
        assert socket.accepted_subprotocol == "clio.browser.v1"
        first = _until_state(socket, lambda s: s["type"] == "state")
        assert first["view_only"] is False and first["url"].endswith("/frames")
        frame, viewport = _frame_and_viewport(socket, first)
        assert frame[:2] == b"\xff\xd8"  # a JPEG
        assert viewport == {"width": 1280, "height": 800}
        box = browser.wait(
            record.page.frame_locator("iframe#results").get_by_role("button").bounding_box()
        )
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        socket.send_text(json.dumps({"type": "mouse", "action": "click", "x": x, "y": y}))
        for _ in range(50):
            text = browser.wait(
                record.page.frame_locator("iframe#results").get_by_role("button").inner_text()
            )
            if text == "Clicked":
                break
            time.sleep(0.1)
        assert text == "Clicked"
        assert record.person_acted is True
        with client.websocket_connect(url, subprotocols=["clio.browser.v1"]) as second:
            other = _until_state(second, lambda s: s["type"] == "state")
            assert other["view_only"] is True
            second.send_text(json.dumps({"type": "navigate", "action": "reload"}))
            ignored = _until_state(second, lambda s: s["type"] == "ignored")
            assert ignored["reason"] == "view_only"


def test_the_person_navigating_grants_the_host_and_bad_input_is_reported(
    browser: Any, site: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_BROWSER_GRANTED_HOSTS", "a.fixture.localhost")
    client, app, sid = _client(tmp_path)
    browser.app = app
    record = browser.wait(browser.open(sid, ""))
    browser.run(record, "goto", lambda r: r.page.goto(site["a"] + "/"))
    url = f"/v1/sessions/{sid}/browser/{record.id}/live"
    with client.websocket_connect(url, subprotocols=["clio.browser.v1"]) as socket:
        _until_state(socket, lambda s: s["type"] == "state")
        socket.send_text("not json")
        assert (
            _until_state(socket, lambda s: s["type"] == "error")["reason"] == "live_message_invalid"
        )
        target = site["b"].removeprefix("http://") + "/table"
        socket.send_text(
            json.dumps({"type": "navigate", "action": "goto", "url": "http://" + target})
        )
        _until_state(socket, lambda s: s.get("host") == "b.fixture.localhost")
    rows = [p for p in app.state.permission_policies if p.get("kind") == "browser_host"]
    assert rows[-1]["host_pattern"] == "b.fixture.localhost" and rows[-1]["scope_id"] == sid


def test_a_viewer_without_the_bearer_or_for_another_session_is_refused(
    browser: Any, tmp_path: Path
) -> None:
    from starlette.websockets import WebSocketDisconnect

    client, app, sid = _client(tmp_path)
    record = browser.wait(browser.open("sess_someone_else", ""))
    with pytest.raises(WebSocketDisconnect) as gone:
        with client.websocket_connect(f"/v1/sessions/{sid}/browser/{record.id}/live") as socket:
            socket.receive_text()
    assert gone.value.code == 4404
    app.state.bearer_token = "secret"
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect(
            f"/v1/sessions/sess_someone_else/browser/{record.id}/live",
            subprotocols=["clio.browser.v1", "clio-bearer.d3Jvbmc"],
        ) as socket:
            socket.receive_text()
    assert refused.value.code == 4401
