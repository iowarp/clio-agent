"""Tests for the shared ``workspace_file`` presentation block (U3).

``view_image``/``view_pdf`` used to declare ``fields:path,...`` (one text row
per field). They now declare
:func:`clio_agent.gact.agents.native_presenters_workspace_file.workspace_file_presentation`,
which renders their descriptor as one ``workspace_file`` block so the
transcript can show what the agent actually saw, as an artifact-style
preview, instead of raw field rows.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pypdf import PdfWriter

from clio_agent.gact import context as gact_context
from clio_agent.gact.agents import tool_instrumentation
from clio_agent.gact.agents.native_presenters_workspace_file import workspace_file_presentation
from clio_agent.gact.view_image_tool import VIEW_IMAGE_DESCRIPTOR_TYPE, build_view_image_tool
from clio_agent.gact.view_pdf_tool import VIEW_PDF_DESCRIPTOR_TYPE, build_view_pdf_tool
from clio_agent.tools.execution import tool_workspace_context

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZQAAAABJRU5ErkJggg=="
)


def _make_pdf(page_count: int) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _with_workspace(
    monkeypatch: Any, *, workspace_id: str = "ws_abc", session_id: str = "sess1"
) -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(sessions={session_id: SimpleNamespace(workspace_id=workspace_id)})
    )
    monkeypatch.setattr(gact_context, "active_app", lambda: app)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: session_id)


def _without_session(monkeypatch: Any) -> None:
    monkeypatch.setattr(gact_context, "active_app", lambda: None)
    monkeypatch.setattr(gact_context, "active_session_id", lambda: "")


def test_view_image_tool_declares_the_workspace_file_presenter() -> None:
    tool = build_view_image_tool()
    assert (
        getattr(tool.func, tool_instrumentation.PRESENTER_ATTR, None) is workspace_file_presentation
    )


def test_view_pdf_tool_declares_the_workspace_file_presenter() -> None:
    tool = build_view_pdf_tool()
    assert (
        getattr(tool.func, tool_instrumentation.PRESENTER_ATTR, None) is workspace_file_presentation
    )


def test_view_image_descriptor_renders_a_workspace_file_block_with_relative_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _with_workspace(monkeypatch)
    nested = tmp_path / "renders"
    nested.mkdir()
    (nested / "page-1.png").write_bytes(_ONE_PIXEL_PNG)
    tool = build_view_image_tool()
    with tool_workspace_context(tmp_path):
        descriptor = tool(path="renders/page-1.png")

    presentation = workspace_file_presentation({"path": "renders/page-1.png"}, descriptor, None)

    assert presentation == {
        "summary": "",
        "blocks": [
            {
                "id": "workspace-file",
                "type": "workspace_file",
                "workspace_id": "ws_abc",
                "path": "renders/page-1.png",
                "media_type": "image/png",
                "sha256": descriptor["sha256"],
            }
        ],
    }
    # The block carries the descriptor's OWN workspace-relative path, never the
    # absolute filesystem location the tool actually resolved on disk.
    block_path = presentation["blocks"][0]["path"]
    assert block_path == "renders/page-1.png"
    assert not Path(block_path).is_absolute()
    assert str(tmp_path) not in block_path


def test_view_pdf_descriptor_renders_a_workspace_file_block_with_resolved_pages(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _with_workspace(monkeypatch, workspace_id="ws_pdf")
    (tmp_path / "doc.pdf").write_bytes(_make_pdf(5))
    tool = build_view_pdf_tool()
    with tool_workspace_context(tmp_path):
        descriptor = tool(path="doc.pdf", pages="2-3")

    assert descriptor["type"] == VIEW_PDF_DESCRIPTOR_TYPE
    presentation = workspace_file_presentation({"path": "doc.pdf", "pages": "2-3"}, descriptor, None)

    assert presentation == {
        "summary": "",
        "blocks": [
            {
                "id": "workspace-file",
                "type": "workspace_file",
                "workspace_id": "ws_pdf",
                "path": "doc.pdf",
                "media_type": "application/pdf",
                "sha256": descriptor["sha256"],
                "pages": [2, 3],
            }
        ],
    }


def test_view_pdf_descriptor_with_no_page_range_lists_every_page(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """An empty ``pages`` range means "the whole document" -- every page was viewed."""

    _with_workspace(monkeypatch)
    (tmp_path / "doc.pdf").write_bytes(_make_pdf(3))
    tool = build_view_pdf_tool()
    with tool_workspace_context(tmp_path):
        descriptor = tool(path="doc.pdf", pages="")

    presentation = workspace_file_presentation({"path": "doc.pdf", "pages": ""}, descriptor, None)

    assert presentation["blocks"][0]["pages"] == [1, 2, 3]


def test_workspace_file_presentation_degrades_to_no_blocks_without_a_valid_descriptor(
    monkeypatch: Any,
) -> None:
    _with_workspace(monkeypatch)

    # Mirrors what the observer passes on a raised ViewImageError/ViewPdfError:
    # ``result=None``, ``structured=None`` (presentation_observer.py appends
    # its own typed error block on top of whatever this presenter returns).
    assert workspace_file_presentation({}, None, None) == {"summary": "", "blocks": []}
    assert workspace_file_presentation({}, {"type": VIEW_IMAGE_DESCRIPTOR_TYPE}, None) == {
        "summary": "",
        "blocks": [],
    }


def test_workspace_file_presentation_resolves_workspace_id_at_call_time(
    monkeypatch: Any,
) -> None:
    """No live app/session -> an empty workspace id, never a stale/fabricated one."""

    _without_session(monkeypatch)
    descriptor = {
        "type": VIEW_IMAGE_DESCRIPTOR_TYPE,
        "path": "a.png",
        "media_type": "image/png",
        "size_bytes": 1,
        "sha256": "0" * 64,
    }

    presentation = workspace_file_presentation({}, descriptor, None)

    assert presentation["blocks"][0]["workspace_id"] == ""
