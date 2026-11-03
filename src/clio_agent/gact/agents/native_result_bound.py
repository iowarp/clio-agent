"""Native tool results get the same result bound as MCP results.

An MCP result is bounded where it is produced (:func:`clio_agent.tools.mcp_result_projection.
bounded_model_tool_result`): past the room one result has in the model's context window
(:mod:`clio_agent.tools.result_room`), the full text goes to the
session's tool-output folder and the agent sees the head and how to read on. Native
tools returned whole until now. :func:`recorded_observation` applies the same rule when
a step is recorded, once, so every later fold of the step sees the same bounded text.

Left whole:

- tools whose full result is what the agent asked for (``tools.full_result``,
  :func:`full_result_tools`: a skill's procedure);
- collected task results, which are already fitted to the parent's room
  (:mod:`clio_agent.gact.child_report_fit`; a report that does not fit arrives as a
  ``result_file``);
- media results, which reach the model as images or documents, not text.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from clio_agent import conf

__all__ = ["full_result_tools", "recorded_observation"]

#: The default ``tools.full_result``.
_FULL_RESULT_TOOLS: Sequence[str] = ("load_skill",)


def full_result_tools() -> frozenset[str]:
    """Tools whose whole result is the point of the call (``tools.full_result``)."""
    return frozenset(
        conf.resolve(
            "tools.full_result",
            env="CLIO_TOOLS_FULL_RESULT",
            default=_FULL_RESULT_TOOLS,
            cast=conf.as_csv,
        )
    )


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
    )
    from clio_agent.tools.result_room import result_room  # noqa: PLC0415

    encoded = _arc_obs_value(value)
    if name in full_result_tools() or isinstance(value, CollectedTaskResult):
        return encoded
    text = encoded if isinstance(encoded, str) else json.dumps(encoded, default=str)
    room = result_room()
    if room.fits(text) or _is_media(value):
        return encoded
    return bounded_model_tool_result(text, room=room)
