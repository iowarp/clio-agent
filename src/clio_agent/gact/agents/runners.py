"""Session-bound execution wrappers for registered DSPy agent modules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx

if TYPE_CHECKING:
    from clio_agent.gact.types import AgentDef


def _app_builder(name: str) -> Any:
    """Resolve a builder through the legacy app seam at execution time."""

    from clio_agent.gact import app as gact_app  # noqa: PLC0415

    return getattr(gact_app, name)


def _runner_kwargs(
    *,
    question: str,
    session_id: str,
    cancel_requested: Any | None,
    images: list[Any] | None,
) -> dict[str, Any]:
    """Build additive runner kwargs without changing text-only call shapes."""

    kwargs: dict[str, Any] = {
        "question": question,
        "session_id": session_id,
        "cancel_requested": cancel_requested,
    }
    if images:
        kwargs["images"] = list(images)
    return kwargs


def _run_blueprint_dspy_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
    images: list[Any] | None = None,
) -> Any:
    """Run a blueprint module with the active session and native images bound."""

    token = _ctx.set_session_id(session_id)
    try:
        module = _app_builder("_build_blueprint_dspy_module")(base_agent, agent_def)
        return module(
            **_runner_kwargs(
                question=question,
                session_id=session_id,
                cancel_requested=cancel_requested,
                images=images,
            )
        )
    finally:
        _ctx.reset(token)


def _run_prompt_user_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
    images: list[Any] | None = None,
) -> Any:
    """Execute a prompt-only user or skill agent through DSPy and LiteLLM."""

    token = _ctx.set_session_id(session_id)
    try:
        module = _app_builder("_build_prompt_user_agent_module")(base_agent, agent_def)
        return module.forward(
            **_runner_kwargs(
                question=question,
                session_id=session_id,
                cancel_requested=cancel_requested,
                images=images,
            )
        )
    finally:
        _ctx.reset(token)


def _run_tool_user_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
    images: list[Any] | None = None,
) -> Any:
    """Execute a tool-declaring user or skill agent through DSPy ReAct."""

    token = _ctx.set_session_id(session_id)
    try:
        module = _app_builder("_build_tool_user_agent_module")(base_agent, agent_def)
        return module.forward(
            **_runner_kwargs(
                question=question,
                session_id=session_id,
                cancel_requested=cancel_requested,
                images=images,
            )
        )
    finally:
        _ctx.reset(token)


__all__ = ["_run_blueprint_dspy_agent", "_run_prompt_user_agent", "_run_tool_user_agent"]
