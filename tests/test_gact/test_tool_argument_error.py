"""A call with arguments the tool does not take is told which ones it does, in a sentence.

On X2 ``memory_read(path=...)`` came back as a 1,000-character traceback ending in "Arg
path is not in the tool's args.", without the argument it takes (``name``).
"""

from __future__ import annotations

from dspy.lm15 import ToolCallPart

from clio_agent.gact.agents import clio_react
from clio_agent.gact.agents.dspy_native_tool import ClioNativeTool, ToolArgumentError


def memory_read(name: str, limit: int = 10) -> str:
    """Read one memory document."""
    return f"read {name}"


def test_the_error_names_the_arguments_the_tool_takes() -> None:
    tool = ClioNativeTool(memory_read)
    try:
        tool(path="user/topics/qy")
    except ToolArgumentError as error:
        text = str(error)
    else:
        raise AssertionError("a call with an unknown argument ran")
    assert "Arg path is not in the tool's args." in text
    assert "name (string, required), limit (integer)" in text


def test_the_loop_gives_the_model_the_sentence_not_a_stack() -> None:
    """SABOTAGE: drop ToolArgumentError from the typed refusals in ``_run_tool`` and
    the observation is a traceback."""
    call = ToolCallPart(id="c1", name="memory_read", input={"path": "user/topics/qy"})
    outcome = clio_react._Loop._run_tool(None, call, ClioNativeTool(memory_read))  # type: ignore[arg-type]
    assert outcome.is_error
    assert "Traceback" not in str(outcome.value)
    assert "Its arguments are: name (string, required)" in str(outcome.value)
