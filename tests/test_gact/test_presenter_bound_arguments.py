"""Observation and presentation receive call arguments, not inspect's variadic container."""

from typing import Any

import pytest

from clio_agent.gact.agents.tool_instrumentation import (
    instrument_tools,
    native_tool,
    present_native_result,
    present_native_start,
)

pytestmark = pytest.mark.usefixtures("clio_core_plane")


def test_variadic_arguments_are_observed_and_presented_unwrapped(monkeypatch: Any) -> None:
    observed: list[dict[str, Any]] = []
    returned = {"content": "unchanged"}

    def execute(**options: Any) -> dict[str, Any]:
        assert options == {"skill_id": "example"}
        return returned

    def presenter(args: Any, result: Any, structured: Any) -> dict[str, Any]:
        assert args == {"skill_id": "example"}
        args["skill_id"] = "presenter-local mutation"
        return {"summary": "Loaded example", "blocks": []}

    def started(args: Any) -> dict[str, Any]:
        assert args == {"skill_id": "example"}
        return {"summary": "Loading example", "blocks": []}

    def observe(name: str, args: Any, phase: str, **payload: Any) -> None:
        observed.append(args)
        if phase == "started":
            presentation = present_native_start(name, args)
            assert presentation is not None
            assert presentation["summary"] == "Loading example"
        else:
            assert payload["result"] == returned
            presentation = present_native_result(name, args, payload["result"], None)
            assert presentation is not None
            assert presentation["summary"] == "Loaded example"

    monkeypatch.setattr("clio_agent.tools.execution.notify_global_tool_observer", observe)
    tool = native_tool(
        execute,
        name="variadic_presentation_test",
        desc="test",
        args={"skill_id": {"type": "string"}},
        presentation=presenter,
        presentation_start=started,
        domain="workspace",
    )
    (tool,) = instrument_tools([tool])
    assert tool.func(skill_id="example") == returned
    assert returned == {"content": "unchanged"}
    assert observed == [{"skill_id": "example"}] * 2


def test_literal_mapping_parameter_is_not_flattened() -> None:
    def execute(kwargs: dict[str, Any]) -> str:
        return "unchanged"

    def presenter(args: Any, result: Any, structured: Any) -> dict[str, Any]:
        assert args == {"kwargs": {"skill_id": "literal"}}
        return {"summary": "Literal mapping", "blocks": []}

    tool = native_tool(
        execute,
        name="literal_mapping_test",
        desc="test",
        args={"kwargs": {"type": "object"}},
        presentation=presenter,
        domain="workspace",
    )
    (tool,) = instrument_tools([tool])
    presentation = present_native_result(
        tool.name, {"kwargs": {"skill_id": "literal"}}, "unchanged", None
    )
    assert presentation is not None
    assert presentation["summary"] == "Literal mapping"


def test_unowned_builtin_can_be_described_but_cannot_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catalog construction cannot introduce an off-application execution fallback."""
    from clio_agent.gact import context

    calls: list[str] = []

    def execute(value: str) -> str:
        calls.append(value)
        return value

    monkeypatch.setattr(context, "active_app", lambda: None)
    (tool,) = instrument_tools(
        [
            native_tool(
                execute,
                name="unowned_probe",
                desc="Probe owned dispatch.",
                args={"value": {"type": "string"}},
                presentation="text",
                domain="workspace",
            )
        ]
    )
    assert (
        tool.format_as_litellm_function_call()["function"]["parameters"]["properties"]["value"][
            "type"
        ]
        == "string"
    )
    with pytest.raises(RuntimeError, match="Built-in MCP transport is not ready"):
        tool.func(value="must not execute")
    assert calls == []
