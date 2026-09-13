"""Presentation receives callable arguments, not inspect's variadic container."""

from typing import Any

from clio_agent.gact.agents.tool_instrumentation import (
    instrument_tools,
    native_tool,
    present_native_result,
    present_native_start,
)


def test_variadic_arguments_are_projected_without_changing_observation(monkeypatch: Any) -> None:
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
            assert present_native_start(name, args)["summary"] == "Loading example"
        else:
            assert payload["result"] is returned
            assert (
                present_native_result(name, args, payload["result"], None)["summary"]
                == "Loaded example"
            )

    monkeypatch.setattr("clio_agent.tools.execution.notify_global_tool_observer", observe)
    tool = native_tool(
        execute,
        name="variadic_presentation_test",
        desc="test",
        args={"skill_id": {"type": "string"}},
        presentation=presenter,
        presentation_start=started,
    )
    instrument_tools([tool])
    assert tool.func(skill_id="example") is returned
    assert observed == [{"options": {"skill_id": "example"}}] * 2


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
    )
    instrument_tools([tool])
    assert (
        present_native_result(tool.name, {"kwargs": {"skill_id": "literal"}}, "unchanged", None)[
            "summary"
        ]
        == "Literal mapping"
    )
