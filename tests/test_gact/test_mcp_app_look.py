"""The agent looks at an app view: the snapshot, the context and the typed refusals (CM1).

``docs/design/sdk/mcp-apps-surfaces-plan.md``, "The agent looks at an app; comments on
app views"; wire shapes in ``mcp-apps-contracts.md``, "Looking at a view".
"""

from __future__ import annotations

import base64
import io
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from clio_agent.gact import context as _ctx
from clio_agent.gact.agents.recorded_media import result_part
from clio_agent.gact.app import build_app
from clio_agent.gact.mcp_app_context import app_context_injection
from clio_agent.gact.mcp_app_look import (
    LOOK_EVENT,
    ViewLooks,
    look_at_view,
    look_table,
    view_context_lines,
)
from clio_agent.gact.mcp_app_look_presentation import present
from clio_agent.gact.mcp_app_sandbox import _SANDBOX_DOCUMENT
from clio_agent.gact.mcp_app_snapshot_script import SNAPSHOT_METHOD
from clio_agent.gact.mcp_apps import MCPAppRecord


def _png(width: int = 40, height: int = 20) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (0, 128, 255)).save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _setup(client: TestClient, app: Any, root: Path) -> tuple[str, MCPAppRecord]:
    workspace = client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()
    sid = client.post("/v1/sessions", json={"workspace_id": workspace["id"]}).json()["id"]
    record = app.state.mcp_app_registry.register(
        session_id=sid,
        source_namespace="scene",
        tool_name="scene_open_view",
        resource_uri="ui://scene/view",
        tool_input={"path": "wave.vti"},
        tool_result={"content": [{"type": "text", "text": "opened"}]},
    )
    return sid, record


def _prefix(sid: str, record: MCPAppRecord) -> tuple[str, dict[str, str]]:
    return f"/v1/sessions/{sid}/mcp-apps/{record.app_instance_id}", {"data_ref": record.data_ref}


def _look(app: Any, sid: str, view: str = "") -> dict[str, Any]:
    tokens = [_ctx.set_app(app), _ctx.set_session_id(sid)]
    try:
        return look_at_view(view)
    finally:
        for token in reversed(tokens):
            _ctx.reset(token)


def test_the_sandbox_document_answers_snapshots_itself() -> None:
    """The host's snapshot request is answered by CLIO's sandbox document, not the view."""
    assert "clioViewSnapshot" in _SANDBOX_DOCUMENT
    assert SNAPSHOT_METHOD in _SANDBOX_DOCUMENT
    assert "@@" not in _SANDBOX_DOCUMENT
    assert "</script>" not in _SANDBOX_DOCUMENT.split("<script>", 2)[1].rsplit("</script>", 1)[0]
    assert "preserveDrawingBuffer" in _SANDBOX_DOCUMENT  # the WebGL path


def test_a_view_no_surface_holds_is_refused_at_once(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid, record = _setup(client, app, tmp_path)
        row = _look(app, sid)
        assert row["ok"] is False and row["reason"] == "mcp_app_view_not_shown"
        assert "scene_open_view" in row["text"]
        view = present({}, row, row)
        assert view["status"] == "failed" and view["summary"] == "The view is not on screen"

        none = _look(app, sid, "molecule")
        assert none["reason"] == "mcp_app_view_none" and none["views"][0]["server"] == "scene"


def test_a_look_gets_the_snapshot_and_the_context_as_model_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid, record = _setup(client, app, tmp_path)
        prefix, cap = _prefix(sid, record)
        report = {
            "content": [{"type": "text", "text": "Isosurface of RTData at 157"}],
            "structuredContent": {"time": 0.5, "camera": {"position": [1, 2, 3]}},
        }
        assert client.put(f"{prefix}/model-context", params=cap, json=report).status_code == 200
        shown = client.put(f"{prefix}/shown", params=cap, json={"holder": "tab", "shown": True})
        assert shown.json() == {"shown": True}
        assert "look_at_view shows you" in app_context_injection(app, sid)

        published: list[Any] = []
        seen = threading.Event()
        original = app.state.bus.publish

        def publish(event: Any) -> None:
            if event.type == LOOK_EVENT:
                published.append(event)
                seen.set()
            original(event)

        monkeypatch.setattr(app.state.bus, "publish", publish)
        monkeypatch.setattr(app.state.bus, "subscriber_count", lambda _sid: 1)
        rows: list[dict[str, Any]] = []
        worker = threading.Thread(target=lambda: rows.append(_look(app, sid, "scene")))
        worker.start()
        assert seen.wait(30)
        payload = published[0].payload
        assert published[0].transient is True and payload["max_side"] == 1568
        bad = client.post(
            f"{prefix}/looks/{payload['call_id']}", params=cap, json={"image": "data:,nope"}
        )
        assert bad.status_code == 422
        answer = {
            "image": _png(),
            "width": 800,
            "height": 600,
            "painted": {"canvas": 1, "text": 3},
            "skipped": [{"what": "canvas", "reason": "webgl_frame_not_redrawn"}],
        }
        posted = client.post(f"{prefix}/looks/{payload['call_id']}", params=cap, json=answer)
        assert posted.status_code == 200
        worker.join(30)
        row = rows[0]
        assert row["type"] == "clio.workspace_image.v1" and row["pixels"] == [40, 20]
        assert row["view"]["server"] == "scene" and row["artifact_id"]
        assert "Isosurface of RTData at 157" in row["model_text"]
        assert 'camera: {"position": [1, 2, 3]}' in row["model_text"]
        assert "webgl_frame_not_redrawn" in row["model_text"]
        again = client.post(f"{prefix}/looks/{payload['call_id']}", params=cap, json=answer)
        assert again.status_code == 404

        tokens = [_ctx.set_app(app), _ctx.set_session_id(sid)]
        try:
            part = result_part("c1", "look_at_view", row, False)
        finally:
            for token in reversed(tokens):
                _ctx.reset(token)
        kinds = [type(item).__name__ for item in part.content]
        assert kinds == ["ImagePart", "TextPart"]
        assert part.content[1].text == row["model_text"]

        view = present({"view": "scene"}, row, row)
        assert view["action"] == "Look at view" and "status" not in view
        assert [block["id"] for block in view["blocks"]] == ["image", "view", "context", "gaps"]


def test_a_refused_snapshot_and_a_closed_view_are_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid, record = _setup(client, app, tmp_path)
        prefix, cap = _prefix(sid, record)
        client.put(f"{prefix}/shown", params=cap, json={"holder": "card", "shown": True})
        calls: list[str] = []
        seen = threading.Event()
        original = app.state.bus.publish

        def publish(event: Any) -> None:
            if event.type == LOOK_EVENT:
                calls.append(event.payload["call_id"])
                seen.set()
            original(event)

        monkeypatch.setattr(app.state.bus, "publish", publish)
        watching = [1]
        monkeypatch.setattr(app.state.bus, "subscriber_count", lambda _sid: watching[0])
        rows: list[dict[str, Any]] = []
        worker = threading.Thread(target=lambda: rows.append(_look(app, sid)))
        worker.start()
        assert seen.wait(30)
        error = {"error": {"reason": "view_not_readable", "message": "another origin"}}
        client.post(f"{prefix}/looks/{calls[0]}", params=cap, json=error)
        worker.join(30)
        assert rows[0]["reason"] == "view_not_readable" and "another origin" in rows[0]["text"]

        seen.clear()
        worker = threading.Thread(target=lambda: rows.append(_look(app, sid)))
        worker.start()
        assert seen.wait(30)
        assert client.delete(prefix, params=cap).status_code == 204
        worker.join(30)
        assert rows[1]["reason"] == "mcp_app_view_closed"


def test_a_look_ends_when_no_window_shows_the_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A browser that closed without unloading its frames cannot answer: the look ends."""
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid, record = _setup(client, app, tmp_path)
        prefix, cap = _prefix(sid, record)
        client.put(f"{prefix}/shown", params=cap, json={"holder": "tab", "shown": True})
        assert _look(app, sid)["reason"] == "mcp_app_view_not_shown"  # nobody watches

        seen = threading.Event()
        original = app.state.bus.publish

        def publish(event: Any) -> None:
            if event.type == LOOK_EVENT:
                seen.set()
            original(event)

        watching = [1]
        monkeypatch.setattr(app.state.bus, "publish", publish)
        monkeypatch.setattr(app.state.bus, "subscriber_count", lambda _sid: watching[0])
        rows: list[dict[str, Any]] = []
        worker = threading.Thread(target=lambda: rows.append(_look(app, sid)))
        worker.start()
        assert seen.wait(30)
        watching[0] = 0
        worker.join(30)
        assert rows[0]["reason"] == "mcp_app_view_not_shown"
        assert not look_table(app).shown(sid, record.app_instance_id)


def test_an_older_mount_unloading_does_not_hide_a_newer_one() -> None:
    table = ViewLooks()
    table.set_shown("s", "a", "tab", True)
    assert table.set_shown("s", "a", "card", False) is True  # the card unloads after the tab
    assert table.shown("s", "a")
    assert table.set_shown("s", "a", "tab", False) is False
    assert not table.shown("s", "a")


def test_context_lines_are_readable_and_cut() -> None:
    lines = view_context_lines(
        {
            "content": [{"type": "text", "text": "Selected residues\nA:12 A:13"}],
            "structuredContent": {"selection": ["A:12", "A:13"], "note": "x" * 50},
        },
        limit=60,
    )
    assert lines[:2] == ["Selected residues", "A:12 A:13"]
    assert lines[2] == 'selection: ["A:12", "A:13"]'
    assert lines[-1].startswith("(cut here:")
    assert view_context_lines(None) == []
    assert isinstance(look_table(SimpleNamespace(state=SimpleNamespace())), ViewLooks)
