"""Native tool schemas are derived once per function shape, and are the same schemas."""

from __future__ import annotations

import types
from types import SimpleNamespace
from typing import Any, Literal

import dspy
import pytest

from clio_agent.gact.agents import dspy_native_tool
from clio_agent.gact.agents.dspy_native_tool import ClioNativeTool


def _fresh(func: Any, arg_desc: dict[str, str] | None = None) -> SimpleNamespace:
    shell = SimpleNamespace(name=None, desc=None, args=None, arg_types=None)
    dspy.Tool._parse_function(shell, func, arg_desc)
    return shell


def _make(default: Any) -> Any:
    def lookup(table: str, limit: int = default, mode: Literal["a", "b"] = "a") -> str:
        """Look a table up."""
        return table

    return lookup


def _build_all() -> dict[str, Any]:
    from clio_agent.gact.agents import native_tool_registry as registry

    agent = types.SimpleNamespace(
        id="probe", tools=list(registry.NATIVE_TOOL_NAMES), metadata={}, parameters={}
    )
    built, _, _ = registry.build_native_tools(
        agent, sorted(registry.NATIVE_TOOL_NAMES), {}, supports_vision=True, supports_pdf=True
    )
    return built


def _shape(tool: Any) -> tuple[Any, ...]:
    return (tool.name, tool.desc, tool.args, tool.arg_types, tool.has_kwargs)


def test_every_native_tool_gets_the_schema_an_uncached_build_gives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent's real native tools, built twice through the cache, against a build
    with the cache off: name, description, schemas and types are equal."""
    monkeypatch.setattr(dspy_native_tool, "_PARSED", {})
    with monkeypatch.context() as off:
        off.setattr(dspy_native_tool, "_parse_key", lambda func, arg_desc: None)
        uncached = {name: _shape(tool) for name, tool in _build_all().items()}
    _build_all()
    cached = {name: _shape(tool) for name, tool in _build_all().items()}
    assert len(dspy_native_tool._PARSED) > 50
    assert cached == uncached


def test_a_new_closure_of_the_same_shape_reuses_the_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """SABOTAGE: make ``_parse_key`` return ``None`` and every tool parses again."""
    parses = []
    original = dspy.Tool._parse_function
    monkeypatch.setattr(dspy_native_tool, "_PARSED", {})

    def counting(self: Any, func: Any, arg_desc: Any = None) -> None:
        parses.append(func)
        original(self, func, arg_desc)

    monkeypatch.setattr(dspy.Tool, "_parse_function", counting)
    first, second = ClioNativeTool(_make(5)), ClioNativeTool(_make(5))
    assert len(parses) == 1
    assert first.args == second.args == _fresh(_make(5)).args
    assert first.args is not second.args
    first.args["table"]["description"] = "changed"
    assert "description" not in second.args["table"]


def test_a_different_default_or_description_is_a_different_schema() -> None:
    five, nine = ClioNativeTool(_make(5)), ClioNativeTool(_make(9))
    assert five.args["limit"]["default"] == 5 and nine.args["limit"]["default"] == 9
    described = ClioNativeTool(_make(5), arg_desc={"table": "Which table."})
    assert described.args["table"]["description"] == "Which table."
    assert "description" not in ClioNativeTool(_make(5)).args["table"]


def test_explicit_args_and_names_still_win() -> None:
    tool = ClioNativeTool(
        _make(5), name="renamed", desc="Own text.", args={"table": {"type": "string"}}
    )
    assert (tool.name, tool.desc) == ("renamed", "Own text.")
    assert tool.args == {"table": {"type": "string"}}
    assert tool.arg_types["limit"] is int
