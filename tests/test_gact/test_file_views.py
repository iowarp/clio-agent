"""Client-rendered file views: the agent looks at them and comments on them reach it (FV).

``docs/design/sdk/mcp-apps-ledger.md``, "Looking at and commenting on client-rendered
files (FV)"; wire shapes in ``mcp-apps-contracts.md``, "Client-rendered file views".
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
from clio_agent.gact.app import build_app
from clio_agent.gact.documents.comment_attachments import (
    CommentAttachment,
    CommentAttachmentError,
    expand_comment_sessions,
)
from clio_agent.gact.documents.file_view_comments import render_file_view_comments
from clio_agent.gact.file_views import (
    FILE_VIEW_LOOK_EVENT,
    MAX_STATE_LINES,
    FileViewInvalid,
    file_view_context_injection,
    state_lines,
    view_file,
)
from clio_agent.gact.mcp_app_look import look_at_view
from clio_agent.gact.mcp_app_look_presentation import present
from clio_agent.gact.parts import Part

_FILE = {
    "workspace_id": "",
    "path": "out/whistle_assembly.step",
    "name": "whistle_assembly.step",
    "kind": "3D model",
}
_STATE = [
    "Model: 2 parts (body, plug); shown size 82.0 × 24.0 × 20.0 mm (as modelled)",
    "Camera: looking from the front (-Y), orthographic",
    "Section: plane at Y = 0.0 mm, keeping the side above (+Y); cut faces filled",
    "Selected part: plug, 20.0 × 24.0 × 20.0 mm",
    "Measurement 1: distance 3.00 mm (ΔX 0.00 ΔY 0.00 ΔZ 3.00), edge to edge",
]


def _png(width: int = 40, height: int = 20) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (0, 128, 255)).save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _session(client: TestClient, root: Path) -> tuple[str, str]:
    workspace = client.post("/v1/workspaces", json={"name": "w", "root_path": str(root)}).json()
    sid = client.post("/v1/sessions", json={"workspace_id": workspace["id"]}).json()["id"]
    return sid, workspace["id"]


def _look(app: Any, sid: str, view: str = "") -> dict[str, Any]:
    tokens = [_ctx.set_app(app), _ctx.set_session_id(sid)]
    try:
        return look_at_view(view)
    finally:
        for token in reversed(tokens):
            _ctx.reset(token)


def test_view_state_and_identity_are_validated_with_typed_reasons() -> None:
    assert state_lines(["  Camera:  front ", "", "Units: mm"]) == ["Camera: front", "Units: mm"]
    assert state_lines(None) == []
    for bad, reason in (
        ("not lines", "file_view_state_invalid"),
        ([1, 2], "file_view_state_invalid"),
        (["x"] * (MAX_STATE_LINES + 1), "file_view_state_too_large"),
        (["x" * 2000], "file_view_state_too_large"),
    ):
        with pytest.raises(FileViewInvalid) as caught:
            state_lines(bad)
        assert caught.value.reason == reason
    assert view_file({"path": "a/b.stl"}) == {
        "workspace_id": "",
        "path": "a/b.stl",
        "name": "b.stl",
        "kind": "file",
    }
    with pytest.raises(FileViewInvalid, match="file_view_identity_missing"):
        view_file({"name": "b.stl"})
    with pytest.raises(FileViewInvalid, match="file_view_identity_invalid"):
        view_file({"path": 3})


def test_a_look_at_a_file_view_gets_the_composite_and_the_view_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid, workspace_id = _session(client, tmp_path)
        prefix = f"/v1/sessions/{sid}/file-views/fv_whistle"
        file = {**_FILE, "workspace_id": workspace_id}
        too_long = client.put(
            prefix, json={"holder": "tab", "shown": True, "file": file, "state": ["x"] * 99}
        )
        assert too_long.status_code == 422
        assert too_long.json()["error"]["error"] == "file_view_state_too_large"
        nameless = client.put(prefix, json={"holder": "tab", "shown": True, "file": {}})
        assert nameless.json()["error"]["error"] == "file_view_identity_missing"
        bad_id = client.put(
            f"/v1/sessions/{sid}/file-views/a%20b",
            json={"holder": "t", "shown": True, "file": file},
        )
        assert bad_id.json()["error"]["error"] == "file_view_id_invalid"

        shown = client.put(
            prefix, json={"holder": "tab", "shown": True, "file": file, "state": _STATE[:2]}
        )
        assert shown.json() == {"shown": True}
        block = file_view_context_injection(app, sid)
        assert "3D model view of out/whistle_assembly.step" in block
        assert "Camera: looking from the front (-Y)" in block

        published: list[Any] = []
        seen = threading.Event()
        original = app.state.bus.publish

        def publish(event: Any) -> None:
            if event.type == FILE_VIEW_LOOK_EVENT:
                published.append(event)
                seen.set()
            original(event)

        monkeypatch.setattr(app.state.bus, "publish", publish)
        monkeypatch.setattr(app.state.bus, "subscriber_count", lambda _sid: 1)
        rows: list[dict[str, Any]] = []
        worker = threading.Thread(target=lambda: rows.append(_look(app, sid, "whistle")))
        worker.start()
        assert seen.wait(30)
        payload = published[0].payload
        assert payload["view_id"] == "fv_whistle" and published[0].transient is True
        bad = client.post(f"{prefix}/looks/{payload['call_id']}", json={"image": "data:,nope"})
        assert bad.status_code == 422
        answer = {
            "image": _png(),
            "width": 900,
            "height": 600,
            "scale": 1,
            "painted": {"canvas": 1, "html": 1},
            "skipped": [],
            "state": _STATE,
        }
        assert client.post(f"{prefix}/looks/{payload['call_id']}", json=answer).status_code == 200
        worker.join(30)
        row = rows[0]
        assert row["type"] == "clio.workspace_image.v1" and row["pixels"] == [40, 20]
        assert row["view"]["kind"] == "file" and row["view"]["path"] == _FILE["path"]
        text = row["model_text"]
        assert "3D model view of the workspace file out/whistle_assembly.step" in text
        assert "The view state when the image was taken:" in text
        assert "Selected part: plug" in text and "distance 3.00 mm" in text
        assert "approximately" not in text  # the file view composites its HTML exactly
        again = client.post(f"{prefix}/looks/{payload['call_id']}", json=answer)
        assert again.status_code == 404

        view = present({"view": "whistle"}, row, row)
        assert [block["id"] for block in view["blocks"]] == ["image", "view", "context"]
        assert view["blocks"][1]["text"] == "3D model view of out/whistle_assembly.step"
        assert view["blocks"][2]["label"] == "View state"


def test_a_file_view_that_is_not_on_screen_is_refused_honestly(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid, workspace_id = _session(client, tmp_path)
        none = _look(app, sid)
        assert none["reason"] == "mcp_app_view_none"
        assert none["text"] == "No app view or file view is open in this conversation."

        prefix = f"/v1/sessions/{sid}/file-views/fv_whistle"
        file = {**_FILE, "workspace_id": workspace_id}
        client.put(prefix, json={"holder": "tab", "shown": True, "file": file, "state": _STATE})
        assert client.put(prefix, json={"holder": "tab", "shown": False}).json() == {"shown": False}
        row = _look(app, sid, "whistle_assembly")
        assert row["reason"] == "mcp_app_view_not_shown"
        assert "not on screen" in row["text"] and "opening the file from Files" in row["text"]
        assert row["view"]["kind"] == "file"
        assert file_view_context_injection(app, sid) == ""  # off screen: no block
        assert present({}, row, row)["summary"] == "The view is not on screen"

        missing = client.put("/v1/sessions/nope/file-views/fv_x", json={"shown": True})
        assert missing.status_code == 404


def _attachment(*, images: list[dict[str, str]] | None = None, **source: Any) -> dict[str, Any]:
    base = {"file": _FILE, "view_state": _STATE, **source}
    return {
        "kind": "file-view",
        "title": "whistle_assembly.step",
        "marks": [
            {
                "label": "S1",
                "comment": "Is this wall thick enough to print?",
                "anchor": {
                    "profile": "region",
                    "rect": [0.4, 0.3, 0.2, 0.25],
                    "natural_width": 900,
                    "natural_height": 600,
                    "source": base,
                },
            },
            {
                "label": "S2",
                "comment": "and here?",
                "anchor": {
                    "profile": "region",
                    "rect": [0.1, 0.1, 0.07, 0.1],
                    "source": {**base, "point": [0.13, 0.15]},
                },
            },
        ],
        "images": images
        if images is not None
        else [{"data": _png()}, {"label": "S1", "data": _png()}, {"label": "S2", "data": _png()}],
    }


def test_a_file_view_comment_renders_the_box_the_state_and_the_crops(tmp_path: Path) -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    attachment = CommentAttachment.model_validate(_attachment())
    rendered = render_file_view_comments(app, "s", attachment, tmp_path, True)  # type: ignore[arg-type]
    text = rendered.text
    assert text.startswith(
        "Comment on the file view “whistle_assembly.step” (a 3D model of the workspace file "
        "out/whistle_assembly.step, drawn in the person's browser as they see it)."
    )
    assert "Image 1 is the view with S1, S2 drawn on it." in text
    assert "x 40% to 60%, y 30% to 55% of the view (180x150 px at (360, 180)" in text
    assert "state when the mark was made:" in text and "Section: plane at Y = 0.0 mm" in text
    assert "as for the mark before" in text
    assert "x 13%, y 15% of the view" in text
    assert "Is this wall thick enough to print?" in text
    assert [caption for caption, _ in rendered.images][1] == "S1 at twice its size"
    assert (tmp_path / "overview.png").exists() and (tmp_path / "S2.png").exists()

    refused = _attachment(
        images=[{"data": _png()}],
        snapshot_refused={"reason": "view_not_ready", "message": "The view is still drawing."},
    )
    text = render_file_view_comments(
        app,  # type: ignore[arg-type]
        "s",
        CommentAttachment.model_validate(refused),
        tmp_path,
        True,
    ).text
    assert "No picture of this mark: The view is still drawing. (view_not_ready)" in text


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"images": [{"data": _png()}, {"data": _png()}]}, "file_view_overview_repeated"),
        ({"images": [{"label": "S7", "data": _png()}]}, "file_view_image_unmatched"),
        ({"images": [{"data": _png(8200, 2)}]}, "file_view_image_too_large"),
        ({"file": {"path": ""}}, "file_view_identity_missing"),
        ({"view_state": ["x" * 900]}, "file_view_state_too_large"),
    ],
)
def test_what_the_browser_made_is_refused_with_a_typed_reason(
    tmp_path: Path, change: dict[str, Any], reason: str
) -> None:
    images = change.pop("images", None)
    attachment = CommentAttachment.model_validate(_attachment(images=images, **change))
    with pytest.raises(CommentAttachmentError, match=reason):
        render_file_view_comments(
            SimpleNamespace(state=SimpleNamespace()),  # type: ignore[arg-type]
            "s",
            attachment,
            tmp_path,
            True,
        )


def test_marks_on_two_files_are_one_session_too_many(tmp_path: Path) -> None:
    payload = _attachment()
    payload["marks"][1]["anchor"]["source"]["file"] = {**_FILE, "path": "out/other.stl"}
    with pytest.raises(CommentAttachmentError, match="file_view_mixed_files"):
        render_file_view_comments(
            SimpleNamespace(state=SimpleNamespace()),  # type: ignore[arg-type]
            "s",
            CommentAttachment.model_validate(payload),
            tmp_path,
            True,
        )


def test_the_comment_round_trip_through_message_admission(tmp_path: Path) -> None:
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=SimpleNamespace())
    with TestClient(app, base_url="http://127.0.0.1:8100") as client:
        sid, workspace_id = _session(client, tmp_path)
        (tmp_path / "out").mkdir()
        payload = _attachment()
        for mark in payload["marks"]:
            mark["anchor"]["source"]["file"] = {**_FILE, "workspace_id": workspace_id}
        session = app.state.sessions.get(sid)
        part = Part(id="c1", type="comment_session", metadata={"comment_session": payload})
        parts, model_text = expand_comment_sessions(app, session, [part], images=True)
        assert [item.type for item in parts] == ["comment_session", "text", *["image"] * 3]
        assert parts[0].metadata["comment_session"]["images"] == []  # images moved out
        assert parts[1].text.startswith("Comments on whistle_assembly.step:")
        assert parts[3].metadata["label"] == "S1 region"
        assert "file_view_file_missing" in model_text  # out/ exists, the STEP does not
        (tmp_path / "out" / "whistle_assembly.step").write_text("ISO-10303-21;")
        _parts, model_text = expand_comment_sessions(app, session, [part], images=False)
        assert "file_view_file_missing" not in model_text
        assert "Images were not sent" in model_text
