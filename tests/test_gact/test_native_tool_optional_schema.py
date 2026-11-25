"""Real native callable defaults survive explicit schemas and observation wrappers."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from clio_agent.gact.a2ui_producer import build_capture_a2ui_surface_tool
from clio_agent.gact.agents.skill_runtime import SkillRuntime, build_load_skill_tool
from clio_agent.gact.agents.tool_instrumentation import instrument_tools, native_tool
from clio_agent.gact.types import AgentDef

pytestmark = pytest.mark.usefixtures("builtin_tool_context")


def test_skill_loader_requires_only_the_skill_identifier() -> None:
    """Bundled file and file-list arguments are optional on the actual skill loader."""
    tool = build_load_skill_tool(AgentDef(id="review", title="Review"), SkillRuntime())
    for candidate in (tool, instrument_tools([tool])[0]):
        parameters = candidate.format_as_litellm_function_call()["function"]["parameters"]
        assert parameters["required"] == ["skill_id"]
        assert set(parameters["properties"]) == {"skill_id", "file", "files"}


def test_keyword_only_defaults_do_not_make_required_arguments_optional() -> None:
    """Use callable defaults while retaining genuinely required keyword-only parameters."""

    def select_rows(dataset: str, *, column: str, limit: int = 10) -> str:
        return f"{dataset}:{column}:{limit}"

    tool = native_tool(
        select_rows,
        name="select_rows",
        desc="Read selected rows.",
        args={
            "dataset": {"type": "string"},
            "column": {"type": "string"},
            "limit": {"type": "integer"},
        },
        presentation="text",
        domain="resources",
        read_only=True,
    )
    parameters = tool.format_as_litellm_function_call()["function"]["parameters"]
    assert parameters["required"] == ["dataset", "column"]
    assert tool(dataset="stations", column="count") == "stations:count:10"


def test_none_default_is_valid_on_wire_and_in_native_execution() -> None:
    """A strict provider can send null without inventing a meaningful epoch zero."""

    def epoch(expected_view_revision: int | None = None) -> int | None:
        return expected_view_revision

    description = "Exact epoch, or null while loading."
    declared = {
        "expected_view_revision": {"type": "integer", "description": description, "default": None}
    }
    tool = native_tool(
        epoch,
        name="epoch",
        desc="Read an epoch.",
        args=declared,
        presentation="text",
        domain="surfaces",
        read_only=True,
    )
    for candidate in (tool, instrument_tools([tool])[0], build_capture_a2ui_surface_tool()):
        properties = candidate.format_as_litellm_function_call()["function"]["parameters"][
            "properties"
        ]
        validator = Draft202012Validator(properties["expected_view_revision"])
        assert validator.is_valid(None)
        assert validator.is_valid(0)
        assert not validator.is_valid("0")
        if candidate.name == "epoch":
            assert properties["expected_view_revision"]["description"] == description
            assert properties["expected_view_revision"]["default"] is None
    assert tool(expected_view_revision=None) is None
    assert tool(expected_view_revision=0) == 0
    assert declared == {
        "expected_view_revision": {"type": "integer", "description": description, "default": None}
    }


@pytest.mark.parametrize(
    "nullable",
    [
        {"anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}]},
        {"oneOf": [{"type": "number", "minimum": 0}, {"type": "null"}]},
        {"type": ["number", "null"], "minimum": 0},
    ],
)
def test_explicit_null_schema_retains_its_shape_and_constraints(nullable: dict[str, Any]) -> None:
    """Native binding preserves existing null alternatives and numeric bounds."""

    def select_limit(limit: float | None = None) -> float | None:
        return limit

    declared = {
        "limit": {**nullable, "description": "Nonnegative limit, or no limit.", "default": None}
    }
    original = copy.deepcopy(declared)
    tool = native_tool(
        select_limit,
        name="select_limit",
        desc="Read the selected limit.",
        args=declared,
        presentation="text",
        domain="resources",
        read_only=True,
    )
    for candidate in (tool, instrument_tools([tool])[0]):
        parameters = candidate.format_as_litellm_function_call()["function"]["parameters"]
        assert parameters["required"] == []
        schema = parameters["properties"]["limit"]
        assert schema == original["limit"]
        validator = Draft202012Validator(schema)
        assert validator.is_valid(None)
        assert validator.is_valid(0)
        assert not validator.is_valid(-1)
        assert not validator.is_valid("0")
    assert tool(limit=None) is None
    assert tool(limit=0) == 0
    assert declared == original


def test_capture_can_be_called_with_only_the_surface_id() -> None:
    """A tool result names the surface it showed; capturing it needs nothing more."""

    parameters = build_capture_a2ui_surface_tool().format_as_litellm_function_call()["function"][
        "parameters"
    ]
    assert parameters.get("required", []) == ["surface_id"]
    validator = Draft202012Validator(parameters["properties"]["expected_revision"])
    assert validator.is_valid(None) and validator.is_valid(3)
