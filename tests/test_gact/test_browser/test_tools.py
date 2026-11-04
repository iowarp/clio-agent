"""The browser tools and the host gate, on a real app and a real Chromium."""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.browser import tools
from clio_agent.gact.browser.presentation import presenter_for
from clio_agent.gact.browser.settings import BrowserError

pytestmark = pytest.mark.timeout(150)  # a real Chromium and real permission questions


@pytest.fixture()
def conversation(browser: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A real app with one session, bound as the active conversation."""
    from clio_agent.gact import context
    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=tmp_path / "sessions.json", arc=None)
    client = TestClient(app)
    sid = client.post("/v1/sessions", json={"title": "Survey"}).json()["id"]
    tokens = [context.set_app(app), context.set_session_id(sid)]
    monkeypatch.setattr(app.state, "permission_policies_path", None, raising=False)
    try:
        yield app, sid
    finally:
        for token in reversed(tokens):
            context.reset(token)


def _answer(
    app: Any, action: str, *, after: float = 0.0, seen: list[dict[str, Any]]
) -> threading.Thread:
    """Answer the next browser_host question with ``action``, as the person would."""
    from clio_agent.gact.permission_gate import resolve_permission

    def run() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            for row in list(app.state.permissions.values()):
                if row.get("kind") == "browser_host" and row.get("status") == "pending":
                    seen.append(dict(row))
                    time.sleep(after)
                    resolve_permission(app, row["id"], action)
                    return
            time.sleep(0.05)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _ungranted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_BROWSER_GRANTED_HOSTS", "")


def test_a_new_host_is_asked_once_and_the_answer_covers_the_conversation(
    conversation: Any, site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _sid = conversation
    _ungranted(monkeypatch)
    seen: list[dict[str, Any]] = []
    answering = _answer(app, "allow_session", seen=seen)
    opened = tools.browser_open(url=site["a"] + "/table")
    answering.join(5)
    assert opened["page"]["host"] == "a.fixture.localhost"
    assert [row["tool_call"]["input"]["host"] for row in seen] == ["a.fixture.localhost"]
    assert seen[0]["summary"] == "Let the browser load a.fixture.localhost"
    again = tools.browser_navigate(opened["page"]["id"], url=site["a"] + "/form")
    assert again["page"]["url"].endswith("/form")
    assert len([r for r in app.state.permissions.values() if r.get("kind") == "browser_host"]) == 1


def test_waiting_for_the_answer_does_not_count_against_the_clock(
    conversation: Any, site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _sid = conversation
    _ungranted(monkeypatch)
    monkeypatch.setenv("CLIO_BROWSER_ACTION_TIMEOUT_S", "1")
    answering = _answer(app, "allow", after=2.5, seen=[])
    opened = tools.browser_open(url=site["a"] + "/")
    answering.join(5)
    assert opened["page"]["host"] == "a.fixture.localhost"


def test_a_denied_redirect_raises_and_the_page_stays(
    conversation: Any, site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _sid = conversation
    monkeypatch.setenv("CLIO_BROWSER_GRANTED_HOSTS", "a.fixture.localhost")
    page = tools.browser_open(url=site["a"] + "/")["page"]["id"]
    seen: list[dict[str, Any]] = []
    answering = _answer(app, "deny", seen=seen)
    with pytest.raises(BrowserError) as denied:
        tools.browser_navigate(page, url=site["a"] + "/redirect-to-b")
    answering.join(5)
    assert denied.value.reason == "browser_host_denied"
    assert [row["tool_call"]["input"]["host"] for row in seen] == ["b.fixture.localhost"]
    assert tools.browser_pages()["pages"][0]["host"] == "a.fixture.localhost"


def test_a_click_that_leads_to_a_denied_host_raises(
    conversation: Any, site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _sid = conversation
    monkeypatch.setenv("CLIO_BROWSER_GRANTED_HOSTS", "a.fixture.localhost")
    page = tools.browser_open(url=site["a"] + "/link-to-b")["page"]["id"]
    answering = _answer(app, "deny", seen=[])
    with pytest.raises(BrowserError) as denied:
        tools.browser_act(page, "click", target={"steps": [{"by": "css", "css": "#go"}]})
    answering.join(5)
    assert denied.value.reason == "browser_host_denied"
    assert tools.browser_pages()["pages"][0]["url"].endswith("/link-to-b")


@pytest.mark.timeout(60)
def test_the_pages_own_navigation_is_refused_and_reported(
    conversation: Any, site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_BROWSER_GRANTED_HOSTS", "a.fixture.localhost")
    page = tools.browser_open(url=site["a"] + "/timed-redirect")["page"]["id"]
    time.sleep(6.5)  # the fixture moves to host b after five seconds, with no call in flight
    result = tools.browser_wait(page, "load")
    assert result["navigation_blocked"]["host"] == "b.fixture.localhost"
    assert result["navigation_blocked"]["reason"] == "browser_host_not_granted"
    assert result["page"]["host"] == "a.fixture.localhost"
    assert "navigation_blocked" not in tools.browser_wait(page, "load")


def test_the_persons_own_navigation_is_granted_for_the_conversation(
    conversation: Any, browser: Any, site: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, sid = conversation
    monkeypatch.setenv("CLIO_BROWSER_GRANTED_HOSTS", "a.fixture.localhost")
    page = tools.browser_open(url=site["a"] + "/")["page"]["id"]
    record = browser.owned(sid, page)
    record.last_person_input = time.monotonic()  # the person typed in the URL bar
    browser.wait(record.page.goto(site["b"] + "/"))
    assert record.host() == "b.fixture.localhost"
    rows = [p for p in app.state.permission_policies if p.get("kind") == "browser_host"]
    assert rows and rows[-1]["host_pattern"] == "b.fixture.localhost"
    assert rows[-1]["scope"] == "session" and rows[-1]["scope_id"] == sid


def test_other_schemes_are_refused(conversation: Any, site: dict[str, Any]) -> None:
    page = tools.browser_open(url=site["a"] + "/")["page"]["id"]
    with pytest.raises(BrowserError):
        tools.browser_navigate(page, url="file:///etc/hostname")
    assert tools.browser_pages()["pages"][0]["host"] == "a.fixture.localhost"


def test_the_agent_never_types_a_password(conversation: Any, site: dict[str, Any]) -> None:
    page = tools.browser_open(url=site["a"] + "/signin")["page"]["id"]
    password = {"steps": [{"by": "label", "text": "Password"}]}
    with pytest.raises(BrowserError) as refused:
        tools.browser_act(page, "fill", target=password, value="hunter2")
    assert refused.value.reason == "browser_password_field"
    assert "sign in" in refused.value.message
    tools.browser_act(
        page, "fill", target={"steps": [{"by": "label", "text": "Username"}]}, value="ada"
    )


def test_a_form_with_select_boxes_and_an_upload(
    conversation: Any, site: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attachment = workspace / "notes.txt"
    attachment.write_text("measured values\n")
    monkeypatch.setenv("CLIO_ALLOWED_ROOTS", str(workspace))
    page = tools.browser_open(url=site["a"] + "/form")["page"]["id"]
    tools.browser_act(
        page, "fill", target={"steps": [{"by": "label", "text": "Title"}]}, value="Run 7"
    )
    tools.browser_act(
        page, "select", target={"steps": [{"by": "label", "text": "Format"}]}, value="Zarr"
    )
    for tag in ("simulation", "archived"):
        tools.browser_act(page, "check", target={"steps": [{"by": "label", "text": tag}]})
    upload = {"steps": [{"by": "label", "text": "Attachment"}]}
    with pytest.raises(BrowserError) as outside:
        tools.browser_act(page, "upload", target=upload, value=str(Path(os.__file__)))
    assert outside.value.reason == "browser_upload_refused"
    tools.browser_act(page, "upload", target=upload, value=str(attachment))
    tools.browser_act(
        page, "click", target={"steps": [{"by": "role", "role": "button", "name": "Submit"}]}
    )
    record = json.loads((site["state"] / "submissions.jsonl").read_text().splitlines()[-1])
    assert record["title"] == "Run 7" and record["format"] == "Zarr"
    assert record["tags"] == ["simulation", "archived"]
    assert record["attachment"] == {"filename": "notes.txt", "size": 16}


def test_snapshot_refs_drive_an_action_and_rows_read_back(
    conversation: Any, site: dict[str, Any]
) -> None:
    page = tools.browser_open(url=site["a"] + "/table")["page"]["id"]
    snap = tools.browser_snapshot(page)
    assert snap["lines"] > 20 and '"Next"' in snap["snapshot"]
    ref = next(
        line.split("[ref=")[1].split("]")[0]
        for line in snap["snapshot"].splitlines()
        if 'link "Next"' in line
    )
    tools.browser_act(page, "click", target={"steps": [{"by": "ref", "ref": ref}]})
    rows = {"steps": [{"by": "role", "role": "row"}, {"by": "filter", "has_text": "Dataset"}]}
    assert tools.browser_read(page, rows, "count")["value"] == 10
    first = {"steps": [*rows["steps"], {"by": "first"}]}
    assert tools.browser_read(page, first, "text")["value"].startswith("Dataset 11")
    view = presenter_for("browser_snapshot")({"page": page}, None, snap)
    assert view["summary"] == f"{snap['lines']} lines"
    assert view["blocks"][0]["type"] == "code" and view["subject"] == "a.fixture.localhost"


def test_a_screenshot_is_written_to_scratch_and_presented(
    conversation: Any, site: dict[str, Any], tmp_path: Path
) -> None:
    page = tools.browser_open(url=site["a"] + "/table")["page"]["id"]
    shot = tools.browser_screenshot(page, full_page=True)
    path = Path(shot["screenshot"]["path"])
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert path.is_relative_to(tmp_path / "scratch")
    view = presenter_for("browser_screenshot")({"page": page}, None, shot)
    assert view["summary"].startswith("PNG")
    assert view["blocks"][0]["id"] == "screenshot"


def test_evaluate_returns_json_and_frames_are_addressable(
    conversation: Any, site: dict[str, Any]
) -> None:
    page = tools.browser_open(url=site["a"] + "/frames")["page"]["id"]
    button = {
        "frame": [{"by": "css", "css": "iframe#results"}],
        "steps": [{"by": "role", "role": "button"}],
    }
    tools.browser_act(page, "click", target=button)
    assert tools.browser_read(page, button, "text")["value"] == "Clicked"
    value = tools.browser_evaluate(page, "() => ({n: document.querySelectorAll('iframe').length})")
    assert value["value"] == {"n": 1}
    view = presenter_for("browser_evaluate")({"page": page, "expression": "() => 1"}, None, value)
    assert [b["type"] for b in view["blocks"]] == ["code", "text"]


def test_a_person_acting_is_reported_once(
    conversation: Any, browser: Any, site: dict[str, Any]
) -> None:
    _app, sid = conversation
    page = tools.browser_open(url=site["a"] + "/")["page"]["id"]
    browser.owned(sid, page).person_acted = True
    first = tools.browser_read(page, {"steps": [{"by": "css", "css": "h1"}]}, "text")
    assert first["person_acted"] is True
    second = tools.browser_read(page, {"steps": [{"by": "css", "css": "h1"}]}, "text")
    assert second["person_acted"] is False
    view = presenter_for("browser_read")(
        {"page": page, "target": {"steps": [{"by": "css", "css": "h1"}]}, "what": "text"},
        None,
        first,
    )
    assert view["subject"] == "text h1 · a.fixture.localhost"
    assert any(b["id"] == "person-acted" for b in view["blocks"])
