"""Real native callable defaults survive explicit schemas and observation wrappers."""

from __future__ import annotations

from clio_agent.gact.agents.skill_runtime import SkillRuntime, build_load_skill_tool
from clio_agent.gact.agents.tool_instrumentation import instrument_tools, native_tool
from clio_agent.gact.types import AgentDef


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
