"""Tool-boundary hook seam types + appliers (P2.3, no-accretion owner module).

The tool-runtime hook *contract* the ``ToolRuntimeHooks`` bundle speaks — the
interceptor decision and the PostToolUse applier — lives here rather than being
appended to the ``execution.py`` god-file. ``execution.py`` imports these and keeps
only THIN call sites (the seam owner still owns the bundle dataclass itself).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InterceptDecision:
    """A tool interceptor's decision for one call (drives the ``tool_interceptor`` slot).

    Produced by a ``PreToolUse`` hook's tagged-union return and consumed by the
    tool boundary:

    * ``kind == "synthesize"`` — SKIP the real tool call and use ``result`` as the
      observation (flagged synthetic; PostToolUse still fires); powers caching /
      mock / offline-replay.
    * ``kind == "modify"`` — run the REAL tool with ``modified_args`` instead of the
      original input.

    A synthesize ``result`` may legitimately be ``None`` — ``kind`` is the
    discriminator, never the value.
    """

    kind: str
    result: Any = None
    modified_args: Mapping[str, Any] | None = None


#: A ``PostToolUse`` hook applied at the tool boundary: given
#: ``(name, args, observation, is_error, synthetic)`` it returns the (possibly
#: rewritten / feedback-appended) observation the MODEL sees. It can never un-run
#: the completed effect — only what enters the model's context.
PostToolHook = Callable[[str, Mapping[str, Any], Any, bool, bool], Any]


def apply_post_tool_hook(
    post_tool: Optional[PostToolHook],
    name: str,
    args: Mapping[str, Any],
    observation: Any,
    *,
    is_error: bool,
    synthetic: bool,
) -> Any:
    """Apply the ``PostToolUse`` hook to the model-visible observation.

    Returns the (possibly rewritten / feedback-appended) observation. Runs AFTER the
    real effect completed and can only change what the model sees — never re-invokes
    the tool. A hook failure never breaks the boundary: the ORIGINAL observation
    stands and a structured reason is logged (no silent fallback).
    """

    if post_tool is None:
        return observation
    try:
        return post_tool(name, dict(args), observation, is_error, synthetic)
    except Exception as exc:  # noqa: BLE001 - a PostToolUse hook must never break the tool boundary
        logger.warning(
            "PostToolUse hook raised; the original observation stands "
            "reason=post_tool_hook_failed tool=%s error=%r",
            name,
            exc,
        )
        return observation
