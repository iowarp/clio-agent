"""Tool-boundary hook seam types + model-observation assembly (no-accretion module).

The tool-runtime hook *contract* the ``ToolRuntimeHooks`` bundle speaks — the
interceptor decision and the PostToolUse applier — lives here rather than being
appended to the ``execution.py`` god-file. ``execution.py`` imports these and keeps
only THIN call sites (the seam owner still owns the bundle dataclass itself).

:func:`assemble_model_observation` is the ONE place the boundary builds what the
MODEL reads back from a completed call. It runs two steps in order, both strictly
after the observer recorded the real effect (the durable trace always keeps the
verbatim result):

1. the artifact identity this call's designated outputs were minted under — the
   registry truth the WIRE lane already carries as ``artifact.created`` + a
   ``resource_link`` part, which the model lane was missing entirely (a staged CSV
   the agent could not cite as ``artifact://<artifact-id>``);
2. the ``PostToolUse`` hook rewrite / deny feedback (:func:`apply_post_tool_hook`).

Neither step can un-run the effect; both only change what enters model context.
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


def merge_artifact_identity(name: str, observation: Any) -> Any:
    """Merge THIS call's minted artifact identity into the model-visible result.

    The model lane's half of designation-by-result: the mint seam published the
    registry-resolved ids for this call on the observer thread, and this consumes
    them into the structured result the model reads back — so an agent can cite
    ``artifact://<artifact-id>`` for a file a tool just wrote instead of having only
    a path. Non-string observations (a raw MCP-Apps result, a synthesized object)
    pass through untouched.

    The merge lives in the artifacts owner module and is imported LAZILY for the
    same reason ``execution.py``'s grounding hook is: this module is imported during
    ``clio_agent.gact`` package init, so a top-level ``clio_agent.gact.artifacts``
    import would re-enter the half-initialized package. A failure never breaks the
    boundary — the ORIGINAL observation stands, with a typed reason logged.
    """

    if not isinstance(observation, str):
        return observation
    try:
        from clio_agent.gact.artifacts.model_identity import (  # noqa: PLC0415
            merge_call_artifact_identity,
        )

        return merge_call_artifact_identity(observation)
    except Exception as exc:  # noqa: BLE001 - identity enrichment must never break the boundary
        logger.warning(
            "artifact identity merge skipped; the original observation stands "
            "reason=artifact_identity_merge_failed tool=%s error=%r",
            name,
            exc,
        )
        return observation


def assemble_model_observation(
    post_tool: Optional[PostToolHook],
    name: str,
    args: Mapping[str, Any],
    observation: Any,
    *,
    is_error: bool,
    synthetic: bool,
) -> Any:
    """Build the model-visible observation for one completed call (the ONE seam).

    Artifact identity first (registry truth about what this call produced), then the
    ``PostToolUse`` hook (a user rewrite must be able to see — and override — the
    enriched result, exactly as it can override the raw one). Returns the
    observation unchanged when neither step contributes anything.
    """

    observation = merge_artifact_identity(name, observation)
    return apply_post_tool_hook(
        post_tool, name, args, observation, is_error=is_error, synthetic=synthetic
    )
