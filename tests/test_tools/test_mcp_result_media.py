"""An MCP tool's image result reaches the model as an image (a ParaView capture, a plot).

Before: the model read ``[image image/png 1280x800]`` and never a pixel. Now each image
block of a model-facing call is snapshotted into the workspace and joins the call's media
slot, so the agent loop attaches it after the result's text.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from dspy.lm15 import ImagePart, TextPart

from clio_agent.gact.agents.clio_react_record import result_part
from clio_agent.gact.model_media import CALL_MEDIA, DETACHED, SCRIPT_MEDIA
from clio_agent.tools.execution import tool_workspace_context
from clio_agent.tools.mcp_result_media import show_to_model

pytestmark = pytest.mark.usefixtures("clio_core_plane")

_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZQAAAABJRU5ErkJggg=="
)


def _result(*blocks: object) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks))


def _image(mime: str = "image/png", data: str = _PNG_B64) -> SimpleNamespace:
    return SimpleNamespace(type="image", mimeType=mime, data=data)


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    with tool_workspace_context(tmp_path):
        yield tmp_path


def test_a_model_calls_image_joins_its_media_and_reaches_the_model(workspace: Path) -> None:
    slot: list[dict] = []
    token = CALL_MEDIA.set(slot)
    try:
        text = SimpleNamespace(type="text", text="ParaView render, 1280x800.")
        shown = show_to_model(_result(text, _image()), "paraview_capture")
    finally:
        CALL_MEDIA.reset(token)
    assert shown == 1
    (descriptor,) = slot
    assert descriptor["label"] == "[image 1 returned by paraview_capture]"
    assert descriptor["media_type"] == "image/png"
    assert (workspace / descriptor["snapshot"]).read_bytes() == base64.b64decode(_PNG_B64) or (
        descriptor["snapshot"].startswith("clio-tool-output:")
    )
    part = result_part(
        "c1", "paraview_capture", "ParaView render [image image/png 1x1]", False, media=slot
    )
    kinds = [type(p) for p in part.content]
    assert kinds == [TextPart, TextPart, ImagePart]
    assert part.content[1].text == "[image 1 returned by paraview_capture]"
    assert part.content[2].data == _PNG_B64


def test_nothing_is_attached_outside_a_model_step(workspace: Path) -> None:
    assert show_to_model(_result(_image()), "paraview_capture") == 0


def test_a_background_script_attaches_nothing(workspace: Path) -> None:
    token = SCRIPT_MEDIA.set(DETACHED)
    try:
        assert show_to_model(_result(_image()), "paraview_capture") == 0
    finally:
        SCRIPT_MEDIA.reset(token)


def test_unsupported_or_corrupt_images_are_skipped(workspace: Path) -> None:
    slot: list[dict] = []
    token = CALL_MEDIA.set(slot)
    try:
        shown = show_to_model(
            _result(
                _image("image/tiff"),
                _image(data="not base64!"),
                {"type": "image", "mimeType": "image/png", "data": _PNG_B64},
            ),
            "plot",
        )
    finally:
        CALL_MEDIA.reset(token)
    assert shown == 1
    assert slot[0]["label"] == "[image 3 returned by plot]"


def test_a_result_without_images_is_untouched(workspace: Path) -> None:
    slot: list[dict] = []
    token = CALL_MEDIA.set(slot)
    try:
        assert show_to_model(_result(SimpleNamespace(type="text", text="ok")), "x") == 0
        assert show_to_model(None, "x") == 0
    finally:
        CALL_MEDIA.reset(token)
    assert slot == []
