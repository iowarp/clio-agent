"""Native workspace-image inspection and provider-wire hydration tests."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import dspy
import pytest
from dspy.lm15 import ImagePart, ToolResultPart

from clio_agent.gact.agents.clio_react import ClioReAct
from clio_agent.gact.agents.declared_native_tools import resolve_declared_native_tools
from clio_agent.gact.types import AgentDef
from clio_agent.gact.view_image_tool import (
    VIEW_IMAGE_DESCRIPTOR_TYPE,
    ViewImageError,
    build_view_image_tool,
)
from clio_agent.tools.execution import tool_workspace_context
from tests._scripted_engine import Reply, calls, scripted_lm

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZQAAAABJRU5ErkJggg=="
)


def _descriptor(tmp_path: Path, name: str = "page-1.png") -> tuple[Any, dict[str, Any]]:
    image_path = tmp_path / name
    image_path.write_bytes(_ONE_PIXEL_PNG)
    tool = build_view_image_tool()
    with tool_workspace_context(tmp_path):
        result = tool(path=name)
    return tool, result


def test_view_image_retains_only_verified_workspace_metadata(tmp_path: Path) -> None:
    _tool, result = _descriptor(tmp_path)

    assert result == {
        "type": VIEW_IMAGE_DESCRIPTOR_TYPE,
        "path": "page-1.png",
        "media_type": "image/png",
        "size_bytes": len(_ONE_PIXEL_PNG),
        "sha256": result["sha256"],
        "snapshot": result["snapshot"],
        "snapshot_sha256": result["sha256"],
    }
    assert len(result["sha256"]) == 64
    assert result["snapshot"].startswith(".clio/tool-output/")
    assert (tmp_path / result["snapshot"]).read_bytes() == _ONE_PIXEL_PNG
    assert "base64" not in str(result).lower()


def test_view_image_refuses_a_file_outside_the_active_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(_ONE_PIXEL_PNG)
    tool = build_view_image_tool()

    with tool_workspace_context(workspace), pytest.raises(ViewImageError) as exc_info:
        tool(path=str(outside))

    assert exc_info.value.reason == "view_image_outside_workspace"


def test_view_image_hydrates_pixels_without_mutating_the_retained_descriptor(
    tmp_path: Path,
) -> None:
    from clio_agent.gact.agents.clio_react_record import result_part

    _tool, result = _descriptor(tmp_path)
    retained = dict(result)
    with tool_workspace_context(tmp_path):
        part = result_part("call_0_0", "view_image", result, False)

    assert result == retained  # the durable descriptor is never expanded in place
    [image] = part.content
    assert isinstance(image, ImagePart)
    assert image.media_type == "image/png"
    assert base64.b64decode(image.data) == _ONE_PIXEL_PNG


def test_a_regenerated_image_still_shows_what_the_agent_saw(tmp_path: Path) -> None:
    """History is what the agent saw: after the file changes (a regenerated plot), an
    earlier view still delivers the bytes it viewed -- never the new file, never a
    failed turn."""
    from clio_agent.gact.agents.clio_react_record import result_part

    _tool, result = _descriptor(tmp_path)
    (tmp_path / "page-1.png").write_bytes(_ONE_PIXEL_PNG + b"changed")

    with tool_workspace_context(tmp_path):
        part = result_part("call_0_0", "view_image", result, False)

    [image] = part.content
    assert isinstance(image, ImagePart)
    assert base64.b64decode(image.data) == _ONE_PIXEL_PNG


def test_media_the_history_cannot_show_is_told_not_fatal(tmp_path: Path) -> None:
    from clio_agent.gact.agents.clio_react_record import result_part

    _tool, result = _descriptor(tmp_path)
    (tmp_path / result["snapshot"]).unlink()

    with tool_workspace_context(tmp_path):
        part = result_part("call_0_0", "view_image", result, False)

    [note] = part.content
    assert note.text.startswith("[clio: media_unavailable] The media this call returned")


def test_the_loop_sends_a_real_image_part_for_the_tool_result(tmp_path: Path) -> None:
    tool, _result = _descriptor(tmp_path)
    lm, engine = scripted_lm([calls(("view_image", {"path": "page-1.png"})), Reply(text="ok")])

    with tool_workspace_context(tmp_path), dspy.context(lm=lm):
        ClioReAct(cast(Any, "question -> answer"), tools=[tool])(question="What is visible?")

    results = [
        part
        for message in engine.requests[1].messages
        for part in message.parts
        if isinstance(part, ToolResultPart)
    ]
    [result] = results
    [media] = result.content
    assert isinstance(media, ImagePart)
    assert (media.media_type, base64.b64decode(media.data)) == ("image/png", _ONE_PIXEL_PNG)
    assert not result.is_error


def test_view_image_is_exposed_only_to_image_capable_agents() -> None:
    agent = AgentDef(id="visual", title="Visual", tools=["view_image"])

    requested_text, available_text, gateway_text = resolve_declared_native_tools(
        agent, {}, supports_vision=False
    )
    requested_image, available_image, gateway_image = resolve_declared_native_tools(
        agent, {}, supports_vision=True
    )

    assert requested_text == []
    assert available_text == {}
    assert gateway_text == []
    assert requested_image == ["view_image"]
    assert list(available_image) == ["view_image"]
    assert gateway_image == []


def test_declared_capability_uses_live_model_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact.agents.declared_native_tools import declared_view_image_capability

    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr("clio_agent.gact.context.active_app", lambda: app)
    monkeypatch.setattr(
        "clio_agent.gact.providers.config._vision_capability",
        lambda _app, provider, model: (provider == "codex" and model == "gpt-5.5", "live"),
    )

    assert declared_view_image_capability(
        SimpleNamespace(provider_id="codex", model="gpt-5.5", supports_vision=False)
    )
    assert not declared_view_image_capability(
        SimpleNamespace(provider_id="codex", model="text-only", supports_vision=True)
    )
