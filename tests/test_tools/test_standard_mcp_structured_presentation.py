"""Generic structured MCP results remain readable without per-tool UI cases."""

import copy
import json
from typing import Any

from clio_agent.gact.tool_result_presentation import recorded_presentation
from clio_agent.tools.tool_presentation import present_mcp_result


def test_structured_only_and_duplicate_text_have_one_readable_exact_result() -> None:
    for content in (
        [],
        [{"type": "text", "text": json.dumps({"account": "Synthetic account", "read_only": True})}],
    ):
        raw = {
            "content": content,
            "structuredContent": {"account": "Synthetic account", "read_only": True},
        }
        original = copy.deepcopy(raw)
        result = present_mcp_result("any_unregistered_mcp", {}, raw)
        assert len(result["blocks"]) == 1
        assert result["blocks"][0]["type"] == "markdown"
        assert (
            result["blocks"][0]["text"] == "- **Account:** Synthetic account\n- **Read only:** true"
        )
        assert result.get("status") is None
        assert raw == original


def test_nested_values_are_bounded_and_cannot_inject_markdown_actions() -> None:
    raw = {
        "content": [],
        "structuredContent": {
            "devices": [
                {"name": "[click](https://example.test)", "value": None} for _ in range(100)
            ]
        },
    }
    result = present_mcp_result("another_mcp", {}, raw)
    text = result["blocks"][0]["text"]
    assert "Devices › 1 › name" in text
    assert r"\[click\]\(https://example\.test\)" in text
    assert ":** null" in text
    assert "More recorded values" in text and text.count("\n- ") < 24


def test_existing_human_content_and_explicit_failures_keep_their_contract() -> None:
    human = present_mcp_result(
        "plain",
        {},
        {
            "content": [{"type": "text", "text": "The report is ready."}],
            "structuredContent": {"id": "one"},
        },
    )
    assert [block["text"] for block in human["blocks"]] == ["The report is ready."]
    failed = present_mcp_result(
        "failed",
        {},
        {"content": [], "isError": True, "structuredContent": {"error": "Permission denied"}},
    )
    assert failed["status"] == "failed"
    assert failed["blocks"][0]["severity"] == "error"


def test_historical_empty_sdk_presentation_uses_exact_retained_result_only() -> None:
    part: dict[str, Any] = {
        "presentation": {"summary": "", "blocks": []},
        "metadata": {"result": {"content": [], "structuredContent": {"read_only": True}}},
    }
    original = copy.deepcopy(part)
    result = recorded_presentation(part)
    assert result and "Read only" in result["blocks"][0]["text"]
    assert part == original
    part["metadata"]["result"]["truncated"] = True
    assert recorded_presentation(part) == part["presentation"]
    part["presentation"]["diagnostic"] = "presentation_failed"
    assert recorded_presentation(part) == part["presentation"]
