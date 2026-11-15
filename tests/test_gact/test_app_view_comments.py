"""Comments on an MCP App view reach the agent as readable entries with crops (CM3)."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image

from clio_agent.gact.documents.app_view_comments import render_app_view_comments
from clio_agent.gact.documents.comment_attachments import (
    CommentAttachment,
    _transcript_summary,
)

_APP = {
    "app_instance_id": "app_1",
    "server": "scene",
    "tool": "open_view",
    "title": "Wave scene",
    "resource_uri": "ui://scene/view",
}


def _png() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 0, 0)).save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _app(stored: dict[str, Any] | None = None) -> Any:
    metadata = {"mcp_app_context": {"app_1": {"context": stored}}} if stored else {}
    session = SimpleNamespace(metadata=metadata)
    return SimpleNamespace(state=SimpleNamespace(sessions={"s": session}))


def _attachment(**extra: Any) -> CommentAttachment:
    context = {
        "content": [{"type": "text", "text": "Isosurface of RTData at 157, step 3 of 10"}],
        "structuredContent": {"time": 0.3, "selection": {"point": 452}},
    }
    return CommentAttachment.model_validate(
        {
            "kind": "mcp-app",
            "title": "Wave scene view",
            "marks": [
                {
                    "label": "S1",
                    "comment": "what is going on in this area?",
                    "anchor": {
                        "profile": "region",
                        "rect": [0.25, 0.5, 0.25, 0.25],
                        "natural_width": 800,
                        "natural_height": 600,
                        "source": {"app": _APP, "context": context, **extra},
                    },
                },
                {
                    "label": "S2",
                    "comment": "and this point?",
                    "anchor": {
                        "profile": "region",
                        "rect": [0.1, 0.1, 0.08, 0.1],
                        "source": {
                            "app": _APP,
                            "context": context,
                            "point": [0.14, 0.15],
                            "snapshot_refused": {
                                "reason": "view_not_readable",
                                "message": "The view runs on another origin.",
                            },
                        },
                    },
                },
            ],
            "images": [{"data": _png()}, {"label": "S1", "data": _png()}],
        }
    )


def test_entries_carry_identity_context_box_and_crop(tmp_path: Path) -> None:
    rendered = render_app_view_comments(_app(), "s", _attachment(), tmp_path, True)
    text = rendered.text
    assert text.startswith(
        "Comment on the view “Wave scene” (scene view, opened by open_view, id app_1)."
    )
    assert "Image 1 is the view with S1, S2 drawn on it." in text
    assert "S1 (region, attached as image 2)" in text
    assert "x 25% to 50%, y 50% to 75% of the view (200x150 px at (200, 300)" in text
    assert "reported when the mark was made:" in text
    assert "Isosurface of RTData at 157, step 3 of 10" in text
    assert 'selection: {"point": 452}' in text
    assert "as reported for the mark before" in text  # S2 shares S1's context
    assert "Point:" in text and "x 14%, y 15% of the view" in text
    assert "No picture of this mark: The view runs on another origin. (view_not_readable)" in text
    assert "Comment:  what is going on in this area?" in text.replace("   ", "  ")
    assert '"app_instance_id"' not in text and "{'" not in text  # no anchor JSON
    assert [caption for caption, _ in rendered.images][1] == "S1 at twice its size"
    assert (tmp_path / "S1.png").exists()


def test_without_images_or_its_own_context_it_says_so(tmp_path: Path) -> None:
    attachment = _attachment()
    for mark in attachment.marks:
        mark.anchor.source.pop("context")
    stored = {"content": [{"type": "text", "text": "Camera at the default view"}]}
    rendered = render_app_view_comments(_app(stored), "s", attachment, tmp_path, False)
    assert rendered.images == []
    assert "Images were not sent" in rendered.text
    assert "as the view last reported it (the mark carried none of its own)" in rendered.text
    assert "Camera at the default view" in rendered.text

    rendered = render_app_view_comments(_app(), "s", attachment, tmp_path, True)
    assert "The view had reported no context." in rendered.text


def test_skipped_parts_and_the_transcript_summary(tmp_path: Path) -> None:
    attachment = _attachment(
        snapshot_skipped=[{"what": "canvas", "reason": "webgl_frame_not_redrawn"}]
    )
    rendered = render_app_view_comments(_app(), "s", attachment, tmp_path, True)
    assert "webgl_frame_not_redrawn" in rendered.text
    summary = _transcript_summary(attachment)
    assert summary.splitlines()[0] == "Comments on Wave scene view:"
    assert "S1 region: what is going on in this area?" in summary
