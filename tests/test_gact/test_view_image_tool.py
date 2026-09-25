"""Native workspace-image inspection and provider-wire hydration tests."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import dspy
import pytest
from dspy.adapters.types.tool import ToolCallResults, ToolCalls

from clio_agent.gact.agents.declared_native_tools import resolve_declared_native_tools
from clio_agent.gact.agents.reactv2 import _RetainingReActV2
from clio_agent.gact.types import AgentDef
from clio_agent.gact.view_image_tool import (
    VIEW_IMAGE_DESCRIPTOR_TYPE,
    ViewImageError,
    build_view_image_tool,
    hydrate_view_image_results,
)
from clio_agent.lm.adapters import _lenient_chat_adapter_cls, _strict_guided_json_adapter_cls
from clio_agent.tools.execution import tool_workspace_context

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


def _history(result: dict[str, Any]) -> dspy.History:
    calls = ToolCalls(
        tool_calls=[
            ToolCalls.ToolCall(id="call_0_0", name="view_image", args={"path": "page-1.png"})
        ]
    )
    results = ToolCallResults.from_tool_calls_and_values(calls, [result], [False])
    return dspy.History(
        messages=[
            {
                "next_thought": "Inspect the rendered page.",
                "tool_calls": calls.model_copy(update={"tool_call_results": results}),
            }
        ]
    )


def test_view_image_retains_only_verified_workspace_metadata(tmp_path: Path) -> None:
    _tool, result = _descriptor(tmp_path)

    assert result == {
        "type": VIEW_IMAGE_DESCRIPTOR_TYPE,
        "path": "page-1.png",
        "media_type": "image/png",
        "size_bytes": len(_ONE_PIXEL_PNG),
        "sha256": result["sha256"],
    }
    assert len(result["sha256"]) == 64
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


def test_view_image_hydrates_pixels_without_mutating_retained_history(tmp_path: Path) -> None:
    _tool, result = _descriptor(tmp_path)
    retained = _history(result)
    inputs: dict[str, Any] = {"history": retained}

    with tool_workspace_context(tmp_path):
        assert hydrate_view_image_results(inputs, "history") == 1

    original_value = retained.messages[0]["tool_calls"].tool_call_results.tool_call_results[0].value
    hydrated_value = (
        inputs["history"].messages[0]["tool_calls"].tool_call_results.tool_call_results[0].value
    )
    assert original_value == result
    assert isinstance(hydrated_value, dspy.Image)
    assert hydrated_value.url.startswith("data:image/png;base64,")


def test_view_image_revalidates_hash_before_provider_delivery(tmp_path: Path) -> None:
    _tool, result = _descriptor(tmp_path)
    (tmp_path / "page-1.png").write_bytes(_ONE_PIXEL_PNG + b"changed")
    inputs: dict[str, Any] = {"history": _history(result)}

    with tool_workspace_context(tmp_path), pytest.raises(ViewImageError) as exc_info:
        hydrate_view_image_results(inputs, "history")

    assert exc_info.value.reason == "view_image_file_changed"


@pytest.mark.parametrize(
    "adapter_class",
    [_lenient_chat_adapter_cls, _strict_guided_json_adapter_cls],
    ids=["lenient-chat", "strict-guided-json"],
)
def test_every_adapter_emits_a_real_image_content_block(tmp_path: Path, adapter_class: Any) -> None:
    tool, result = _descriptor(tmp_path)
    react = _RetainingReActV2(cast(Any, "question -> answer"), tools=[tool])
    adapter = adapter_class()()
    inputs = {
        "question": "What is visible?",
        "history": _history(result),
        "tools": [tool],
    }

    with tool_workspace_context(tmp_path), dspy.context(adapter=adapter):
        messages = adapter.format(react.react.signature, [], inputs)

    blocks = [
        block
        for message in messages
        for block in (message.get("content") if isinstance(message.get("content"), list) else [])
        if isinstance(block, dict)
    ]
    assert any(block.get("type") == "image_url" for block in blocks)


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
        lambda _app, provider, model: (provider == "chatgpt" and model == "gpt-5.5", "live"),
    )

    assert declared_view_image_capability(
        SimpleNamespace(provider_id="chatgpt", model="gpt-5.5", supports_vision=False)
    )
    assert not declared_view_image_capability(
        SimpleNamespace(provider_id="chatgpt", model="text-only", supports_vision=True)
    )
