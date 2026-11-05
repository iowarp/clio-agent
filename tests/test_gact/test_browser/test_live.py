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


def test_the_person_signs_in_through_the_live_view(
    browser: Any, site: dict[str, Any], tmp_path: Path
) -> None:
    client, _app, sid = _client(tmp_path)
    record = browser.wait(browser.open(sid, ""))
    browser.run(record, "goto", lambda r: r.page.goto(site["a"] + "/signin"))
    boxes = {
        name: browser.wait(record.page.locator(selector).bounding_box())
        for name, selector in (("user", "input[name=username]"), ("pass", "input[type=password]"))
    }
    url = f"/v1/sessions/{sid}/browser/{record.id}/live"
    with client.websocket_connect(url, subprotocols=["clio.browser.v1"]) as socket:
        _until_state(socket, lambda s: s["type"] == "state")
        for name, text in (("user", "ada"), ("pass", "correct horse")):
            box = boxes[name]
            x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
            for action in ("down", "up"):  # as the live view sends a click
                socket.send_text(json.dumps({"type": "mouse", "action": action, "x": x, "y": y}))
            socket.send_text(json.dumps({"type": "text", "text": text}))
        for _ in range(30):
            values = [
                browser.wait(record.page.locator(s).input_value())
                for s in ("input[name=username]", "input[type=password]")
            ]
            if values == ["ada", "correct horse"]:
                break
            time.sleep(0.1)
        assert values == ["ada", "correct horse"], values
        socket.send_text(json.dumps({"type": "key", "action": "press", "key": "Enter"}))
        _until_state(socket, lambda s: s.get("url", "").endswith("/account"))
    assert record.url().endswith("/account")


def test_the_page_lays_out_at_the_views_size(
    browser: Any, site: dict[str, Any], tmp_path: Path
) -> None:
    """A narrow panel shows the page's own narrow layout, not a shrunken wide frame."""
    client, _app, sid = _client(tmp_path)
    record = browser.wait(browser.open(sid, "Narrow"))
    browser.run(record, "goto", lambda r: r.page.goto(site["a"] + "/table"))
    url = f"/v1/sessions/{sid}/browser/{record.id}/live"
    with client.websocket_connect(url, subprotocols=["clio.browser.v1"]) as socket:
        _until_state(socket, lambda s: s["type"] == "state")
        socket.send_text(json.dumps({"type": "resize", "width": 390, "height": 700}))
        state = _until_state(socket, lambda s: (s.get("viewport") or {}).get("width") == 390)
        assert state["viewport"] == {"width": 390, "height": 700}
        assert record.page.viewport_size == {"width": 390, "height": 700}
        assert record.person_acted is False  # resizing the view is not acting on the page
        for _ in range(100):
            if socket.receive().get("bytes"):
                break  # frames keep coming at the new size
        else:
            raise AssertionError("no frame after the resize")


def test_a_link_the_person_opens_is_their_navigation(
    browser: Any, site: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conversation link opens in the shared browser; its host is the person's to grant."""
    monkeypatch.setenv("CLIO_BROWSER_GRANTED_HOSTS", "")
    client, app, sid = _client(tmp_path)
    browser.app = app
    url = site["b"] + "/table"
    opened = client.post(f"/v1/sessions/{sid}/browser/pages", json={"url": url})
    assert opened.status_code == 200, opened.text
    page = opened.json()["page"]["id"]
    record = browser.owned(sid, page)
    for _ in range(100):
        if record.host() == "b.fixture.localhost":
            break
        time.sleep(0.05)
    assert record.host() == "b.fixture.localhost"
    rows = [p for p in app.state.permission_policies if p.get("kind") == "browser_host"]
    assert rows[-1]["host_pattern"] == "b.fixture.localhost" and rows[-1]["scope_id"] == sid
    again = client.post(f"/v1/sessions/{sid}/browser/pages", json={"url": url}).json()
    assert again["page"]["id"] == page  # the page already on that address
    assert (
        client.post(
            f"/v1/sessions/{sid}/browser/pages", json={"url": "file:///etc/hosts"}
        ).status_code
        == 422
    )


def test_an_html_file_the_person_opens_loads_as_a_local_page(
    browser: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files opens an HTML file in the shared browser by its path, inside the readable roots."""
    client, app, sid = _client(tmp_path)
    browser.app = app
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = client.post(
        "/v1/workspaces", json={"name": "figures", "root_path": str(root)}
    ).json()
    sid = client.post(
        "/v1/sessions", json={"title": "Figures", "workspace_id": workspace["id"]}
    ).json()["id"]
    (root / "figs").mkdir()
    figure = root / "figs" / "scene.html"
    figure.write_text("<title>Scene</title><p>three</p>")
    opened = client.post(f"/v1/sessions/{sid}/browser/pages", json={"path": "figs/scene.html"})
    assert opened.status_code == 200, opened.text
    record = browser.owned(sid, opened.json()["page"]["id"])
    for _ in range(100):
        if record.url() == figure.as_uri():
            break
        time.sleep(0.05)
    assert record.url() == figure.as_uri()
    figure.write_text("<title>Scene v2</title><p>three</p>")  # the file changed on disk
    again = client.post(f"/v1/sessions/{sid}/browser/pages", json={"path": "figs/scene.html"})
    assert again.json()["page"]["id"] == record.id  # the same tab, reloaded
    for _ in range(100):
        if record.title == "Scene v2":
            break
        time.sleep(0.05)
    assert record.title == "Scene v2"
    missing = client.post(f"/v1/sessions/{sid}/browser/pages", json={"path": "figs/none.html"})
    assert missing.status_code == 422 and "browser_file_missing" in missing.text
