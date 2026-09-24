"""Per-message reasoning effort for one turn.

The composer sends ``behavior.reasoning_effort`` with every message, and
``message_submission`` stores it on the user message, but nothing read it: only
the global ``thinking_level`` ever reached the LM. This module applies it.

The level is overlaid onto the turn's LOCAL agent copy as
``parameters["thinking_level"]`` — the same per-expert override
:func:`clio_agent.providers.lm_spec.build_spec` already honors — so it flows
through the ordinary spec -> resolver -> ``create_lm`` chain and is translated
by :func:`clio_agent.providers.thinking.resolve_thinking` (the single level ->
kwargs mapping) into the provider-correct kwargs of the LM that runs under the
turn's ``dspy.context``. Nothing global is mutated; the next turn without a
per-message level runs on the configured level again.

:func:`turn_reasoning_provenance` records what was requested and what the
provider will actually receive, so the effective level is visible in the
turn's ``agent_runtime`` provenance (the ``llm.request.started`` trace payload).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef, Message

#: ``AgentDef.metadata`` key naming the layer that chose this turn's level.
TURN_REASONING_SOURCE_KEY = "turn_reasoning_source"


def message_reasoning_effort(user_msg: "Message") -> str:
    """Return the per-message reasoning effort stored on ``user_msg``, or ``""``."""

    metadata = user_msg.metadata if isinstance(user_msg.metadata, dict) else {}
    behavior = metadata.get("behavior")
    if not isinstance(behavior, dict):
        return ""
    return str(behavior.get("reasoning_effort") or "").strip().lower()


def apply_turn_reasoning(user_msg: "Message", agent_def: "AgentDef") -> "AgentDef":
    """Overlay the message's reasoning effort onto a LOCAL copy of ``agent_def``.

    Returns ``agent_def`` unchanged when the message carries no level (the
    agent's own or the global level then governs).
    """

    level = message_reasoning_effort(user_msg)
    if not level:
        return agent_def
    parameters = dict(agent_def.parameters or {})
    parameters["thinking_level"] = level
    metadata = dict(agent_def.metadata)
    metadata[TURN_REASONING_SOURCE_KEY] = "per_message"
    return agent_def.model_copy(update={"parameters": parameters, "metadata": metadata})


def turn_reasoning_provenance(
    app: "FastAPI", agent_def: "AgentDef", provider_id: str
) -> dict[str, Any]:
    """Describe the thinking level this turn's LM runs with (non-secret).

    Mirrors how :func:`~clio_agent.providers.lm_spec.build_spec` resolves the
    level (the agent's ``parameters`` over the active default profile) and asks
    :func:`~clio_agent.providers.thinking.resolve_thinking` what the provider
    receives for it.
    """

    from clio_agent.gact.providers.config import (  # noqa: PLC0415
        _effective_lm_config,
        _provider_runtime_kind,
    )
    from clio_agent.providers.thinking import resolve_thinking  # noqa: PLC0415

    active = _effective_lm_config(app)
    parameters = agent_def.parameters or {}
    requested = str(parameters.get("thinking_level") or "").strip().lower()
    source = str(agent_def.metadata.get(TURN_REASONING_SOURCE_KEY) or "")
    if requested and not source:
        source = "agent_default"
    if not requested:
        requested = str(active.get("thinking_level") or "").strip().lower()
        source = "global" if requested else "provider_default"
    budget = int(parameters.get("thinking_budget") or active.get("thinking_budget") or 0)
    provider_kind = _provider_runtime_kind(provider_id) or str(active.get("provider") or "")
    try:
        plan = resolve_thinking(provider_kind, requested or None, budget)
    except ValueError as exc:
        return {
            "requested_level": requested,
            "source": source,
            "provider": provider_kind,
            "effective_level": "invalid",
            "reason": str(exc),
        }
    kwargs = dict(plan.litellm_kwargs)
    if plan.sdk_thinking is not None:
        kwargs["claude_code_thinking"] = plan.sdk_thinking
    record: dict[str, Any] = {
        "requested_level": requested,
        "source": source,
        "provider": provider_kind,
        "effective_level": plan.effective_level,
        "lm_kwargs": kwargs,
    }
    if not plan.supported:
        record["reason"] = plan.unsupported_reason or ""
    return record


__all__ = [
    "TURN_REASONING_SOURCE_KEY",
    "apply_turn_reasoning",
    "message_reasoning_effort",
    "turn_reasoning_provenance",
]
