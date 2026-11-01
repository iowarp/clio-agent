"""Native tool results get the same result bound as MCP results.

An MCP result is bounded where it is produced (:func:`clio_agent.tools.mcp_result_projection.
bounded_model_tool_result`): over the model's result size, the full text goes to the
session's tool-output folder and the agent sees the head and how to read on. Native
tools returned whole until now. :func:`recorded_observation` applies the same rule when
a step is recorded, once, so every later fold of the step sees the same bounded text.

Left whole:

- tools whose full result is what the agent asked for (:data:`FULL_RESULT_TOOLS`:
  a skill's procedure);
- collected task results, which are already bounded per task (a result over the digest
  size arrives as a ``result_file``);
- media results, which reach the model as images or documents, not text.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["FULL_RESULT_TOOLS", "recorded_observation"]

#: Tools whose whole result is the point of the call.
FULL_RESULT_TOOLS = frozenset({"load_skill"})


def _is_media(value: Any) -> bool:
    from clio_agent.gact.agents.recorded_media import _media  # noqa: PLC0415

    try:
        return _media(value) is not None
    except ValueError:
        return True  # media whose bytes are gone is still media, not text to bound


def recorded_observation(name: str, value: Any) -> Any:
    """The observation to record for one call: the value, or its bounded text."""
    from clio_agent.gact.runtime.context_tokens import _arc_obs_value  # noqa: PLC0415
    from clio_agent.gact.tool_observation import CollectedTaskResult  # noqa: PLC0415
    from clio_agent.tools.mcp_result_projection import (  # noqa: PLC0415
        bounded_model_tool_result,
        model_tool_result_chars,
    )

    encoded = _arc_obs_value(value)
    if name in FULL_RESULT_TOOLS or isinstance(value, CollectedTaskResult):
        return encoded
    text = encoded if isinstance(encoded, str) else json.dumps(encoded, default=str)
    if len(text) <= model_tool_result_chars() or _is_media(value):
        return encoded
    return bounded_model_tool_result(text)
