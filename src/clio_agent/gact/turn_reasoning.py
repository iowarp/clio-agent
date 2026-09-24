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

Spawned children
----------------
A child session's first message carries no level of its own. When the parent's
turn ran on a PER-MESSAGE level, the child inherits that level if -- and only
if -- it resolves to the same provider and model as the parent turn (a level is
a property of one model's effort scale; ``max`` on Fable means nothing on
gpt-oss). Otherwise the child runs on its own/global level and its provenance
records ``inherited: false, reason: "different_model"``. The parent's level is
kept per parent TURN by :func:`record_turn_reasoning` (in memory, bounded, pruned
with its session) and the child finds it through the ``parent_turn_id`` its
AgentTask captured at spawn -- so a later turn of the parent cannot change what
an already-spawned background child inherits.
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


#: ``AgentDef.metadata`` key recording whether a child inherited its parent's level.
TURN_REASONING_INHERITANCE_KEY = "turn_reasoning_inheritance"

_TURN_REASONING_BY_TURN = "turn_reasoning_by_turn"

#: Parent-turn records kept in memory (oldest dropped first).
TURN_REASONING_CAPACITY = 512


def _turn_model(app: "FastAPI | None", agent_def: "AgentDef") -> tuple[str, str]:
    """The (provider_id, model_id) this turn runs on, resolved like its provenance."""

    active: dict[str, str] = {}
    if app is not None:
        from clio_agent.gact.providers.config import _active_lm_model_ref  # noqa: PLC0415

        active = _active_lm_model_ref(app)
    return (
        agent_def.default_provider or active.get("provider_id", ""),
        agent_def.default_model or active.get("model_id", ""),
    )


def _with_level(agent_def: "AgentDef", level: str, **meta: Any) -> "AgentDef":
    parameters = dict(agent_def.parameters or {})
    parameters["thinking_level"] = level
    metadata = {**agent_def.metadata, **meta}
    return agent_def.model_copy(update={"parameters": parameters, "metadata": metadata})


def _registry(app: "FastAPI") -> dict[str, dict[str, str]]:
    registry = getattr(app.state, _TURN_REASONING_BY_TURN, None)
    if not isinstance(registry, dict):
        registry = {}
        setattr(app.state, _TURN_REASONING_BY_TURN, registry)
    return registry


def record_turn_reasoning(
    app: "FastAPI", session_id: str, turn_id: str, runtime: dict[str, Any]
) -> None:
    """Remember the level a turn runs on, for the children it spawns."""

    registry = _registry(app)
    raw_model, raw_reasoning = runtime.get("model"), runtime.get("reasoning")
    model: dict[str, Any] = raw_model if isinstance(raw_model, dict) else {}
    reasoning: dict[str, Any] = raw_reasoning if isinstance(raw_reasoning, dict) else {}
    registry[turn_id] = {
        "session_id": session_id,
        "provider_id": str(model.get("provider_id") or ""),
        "model_id": str(model.get("model_id") or ""),
        "level": str(reasoning.get("requested_level") or ""),
        "source": str(reasoning.get("source") or ""),
    }
    while len(registry) > TURN_REASONING_CAPACITY:
        registry.pop(next(iter(registry)))


def forget_session_reasoning(app: "FastAPI", session_id: str) -> None:
    """Drop the turn records of a deleted session."""

    registry = _registry(app)
    for turn_id in [t for t, row in registry.items() if row.get("session_id") == session_id]:
        registry.pop(turn_id, None)


def _parent_turn_reasoning(app: "FastAPI | None", user_msg: "Message") -> dict[str, str] | None:
    metadata = user_msg.metadata if isinstance(user_msg.metadata, dict) else {}
    task_id = str(metadata.get("agent_task_id") or "")
    tasks = getattr(getattr(app, "state", None), "agent_task_registry", None)
    task = tasks.get(task_id) if task_id and tasks is not None else None
    parent_turn_id = str(getattr(task, "parent_turn_id", "") or "")
    if app is None or not parent_turn_id:
        return None
    record = _registry(app).get(parent_turn_id)
    if not isinstance(record, dict) or not record.get("level"):
        return None
    # Only a level the parent's MESSAGE chose is inherited; an agent/global level
    # already governs the child the same way.
    if record.get("source") not in {"per_message", "parent_message"}:
        return None
    return record


def apply_turn_reasoning(
    user_msg: "Message",
    agent_def: "AgentDef",
    *,
    app: "FastAPI | None" = None,
    session: Any = None,
) -> "AgentDef":
    """Overlay this turn's reasoning effort onto a LOCAL copy of ``agent_def``.

    The message's own level wins. A spawned child without one inherits its
    parent turn's per-message level when it runs on the same provider+model
    (see the module docstring); otherwise the non-inheritance is recorded.
    Returns ``agent_def`` unchanged when no level applies.
    """

    level = message_reasoning_effort(user_msg)
    if level:
        return _with_level(agent_def, level, **{TURN_REASONING_SOURCE_KEY: "per_message"})
    del session  # the parent turn is found through the child's AgentTask
    parent = _parent_turn_reasoning(app, user_msg)
    if parent is None:
        return agent_def
    if _turn_model(app, agent_def) == (parent["provider_id"], parent["model_id"]):
        return _with_level(
            agent_def,
            parent["level"],
            **{
                TURN_REASONING_SOURCE_KEY: "parent_message",
                TURN_REASONING_INHERITANCE_KEY: {"inherited": True},
            },
        )
    metadata = {
        **agent_def.metadata,
        TURN_REASONING_INHERITANCE_KEY: {
            "inherited": False,
            "reason": "different_model",
            "parent_level": parent["level"],
        },
    }
    return agent_def.model_copy(update={"metadata": metadata})


def turn_reasoning_provenance(
    app: "FastAPI", agent_def: "AgentDef", provider_id: str, model_id: str = ""
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
    from clio_agent.providers.reasoning_levels import model_effort_levels  # noqa: PLC0415
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
        plan = resolve_thinking(
            provider_kind,
            requested or None,
            budget,
            effort_levels=model_effort_levels(provider_kind, model_id),
        )
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
    inheritance = agent_def.metadata.get(TURN_REASONING_INHERITANCE_KEY)
    if isinstance(inheritance, dict):
        record.update(inheritance)
    return record


__all__ = [
    "TURN_REASONING_INHERITANCE_KEY",
    "TURN_REASONING_SOURCE_KEY",
    "apply_turn_reasoning",
    "forget_session_reasoning",
    "record_turn_reasoning",
    "message_reasoning_effort",
    "turn_reasoning_provenance",
]
