"""A native tool's arguments: null for a defaulted argument, and calls that fail validation."""

from __future__ import annotations

from typing import Any

import pytest

from clio_agent.gact.agents.dspy_native_tool import ClioNativeTool
from clio_agent.gact.agents.tool_recording import recording_tool
from clio_agent.tools.execution import ToolRuntimeHooks, set_tool_runtime_fallback


def _load(skill_id: str, file: str = "", files: list[str] | None = None) -> str:
    return f"{skill_id}|{file}|{files}"


def _tool() -> ClioNativeTool:
    return ClioNativeTool(
        _load,
        name="load_skill",
        args={
            "skill_id": {"type": "string"},
            "file": {"type": "string", "default": ""},
            "files": {"type": "array", "items": {"type": "string"}, "default": None},
        },
    )


def _observed() -> list[tuple[str, dict[str, Any], str, str | None]]:
    seen: list[tuple[str, dict[str, Any], str, str | None]] = []
    set_tool_runtime_fallback(
        ToolRuntimeHooks(
            tool_observer=lambda name, args, phase, error, result=None: seen.append(
                (name, dict(args), phase, error)
            )
        )
    )
    return seen


def test_null_for_an_argument_with_a_default_takes_the_default() -> None:
    seen = _observed()
    try:
        assert _tool()(skill_id="start-project", file=None, files=None) == "start-project||None"
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
    assert seen == []


def test_a_call_with_invalid_arguments_is_recorded_with_its_reason() -> None:
    seen = _observed()
    try:
        with pytest.raises(ValueError, match="Arg file is invalid"):
            _tool()(skill_id="start-project", file=3)
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
    args = {"skill_id": "start-project", "file": 3}
    assert seen[0] == ("load_skill", args, "started", None)
    assert seen[1][:3] == ("load_skill", args, "completed")
    assert seen[1][3].startswith("invalid arguments: Arg file is invalid")


def test_a_recorded_tool_takes_the_default_for_null() -> None:
    # load_skill reaches the agent wrapped by recording_tool; the wrapper validates first.
    seen = _observed()
    try:
        recorded = recording_tool(_tool())
        assert recorded(skill_id="start-project", file=None, files=None) == "start-project||None"
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
    assert seen == []


def test_a_recorded_tool_records_a_call_with_invalid_arguments() -> None:
    seen = _observed()
    try:
        with pytest.raises(ValueError, match="Arg file is invalid"):
            recording_tool(_tool())(skill_id="start-project", file=3)
    finally:
        set_tool_runtime_fallback(ToolRuntimeHooks())
    assert [row[2] for row in seen] == ["started", "completed"]
    assert seen[1][3].startswith("invalid arguments: Arg file is invalid")
