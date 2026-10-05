"""Viewed document pixels survive canonical state outside the workspace."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest

from clio_agent import paths
from clio_agent.gact import viewed_media
from clio_agent.gact.view_image_tool import _descriptor, _hydrate_descriptor
from clio_agent.tools.execution import tool_workspace_context


def test_managed_snapshot_hydrates_pixels_outside_authored_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "managed-state"
    monkeypatch.setattr(paths, "workspace_clio", lambda root: state)
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZQAAAABJRU5ErkJggg=="
    )
    (workspace / "page.png").write_bytes(image)
    with tool_workspace_context(workspace):
        descriptor = _descriptor("page.png")
        assert descriptor["snapshot"].startswith("clio-tool-output:")
        assert viewed_media.read_snapshot(descriptor["snapshot"], descriptor["sha256"]) == image
        hydrated, size = _hydrate_descriptor(descriptor)
        assert size == len(image)
        assert hydrated.url.startswith("data:image/png;base64,")
        (workspace / "page.png").unlink()
        assert viewed_media.read_snapshot(descriptor["snapshot"], descriptor["sha256"]) == image
    assert list((state / "tool-output").glob("viewed-*.png"))


def test_selected_pdf_pages_hydrate_from_external_managed_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pypdf import PdfReader, PdfWriter

    from clio_agent.gact import view_pdf_tool

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(paths, "workspace_clio", lambda root: tmp_path / "managed-state")
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    with (workspace / "document.pdf").open("wb") as output:
        writer.write(output)
    with tool_workspace_context(workspace):
        descriptor = view_pdf_tool._descriptor("document.pdf", "2")
        data = viewed_media.read_snapshot(descriptor["snapshot"], descriptor["snapshot_sha256"])
        assert len(PdfReader(io.BytesIO(data)).pages) == 1
        _document, size = view_pdf_tool._hydrate_descriptor(descriptor)
        assert size == len(data)


@pytest.mark.parametrize("reference", ["../outside", "clio-tool-output:../outside"])
def test_snapshot_reference_cannot_escape_bound_storage(tmp_path: Path, reference: str) -> None:
    with (
        tool_workspace_context(tmp_path),
        pytest.raises(viewed_media.ViewedMediaUnavailable, match="escapes"),
    ):
        viewed_media.read_snapshot(reference, "0" * 64)
