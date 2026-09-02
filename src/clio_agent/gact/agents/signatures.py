"""DSPy signatures shared by registered prompt and tool agents."""

from __future__ import annotations

from typing import Any

import dspy


class PromptUserAgentSignature(dspy.Signature):
    """Run a registered CLIO user agent using supplied runtime instructions."""

    system_prompt: str = dspy.InputField(desc="Registered agent instructions")
    question: str = dspy.InputField(desc="User message for this agent")
    images: list[dspy.Image] = dspy.InputField(desc="User-provided images for this turn")
    answer: str = dspy.OutputField(desc="User-facing answer")
    expert_handoffs: str = dspy.OutputField(
        desc=(
            "JSON array of synchronous child expert delegations to execute next. "
            "Use [] when no child expert should be called."
        )
    )


class ToolUserAgentSignature(dspy.Signature):
    """Run a registered CLIO user agent using supplied tool runtime instructions."""

    system_prompt: str = dspy.InputField(desc="Registered agent instructions")
    question: str = dspy.InputField(desc="User message for this agent")
    images: list[dspy.Image] = dspy.InputField(desc="User-provided images for this turn")
    answer: str = dspy.OutputField(desc="User-facing answer")
    expert_handoffs: str = dspy.OutputField(
        desc=(
            "JSON array of synchronous child expert delegations to execute next. "
            "Use [] when no child expert should be called."
        )
    )


def _prompt_user_agent_signature() -> Any:
    """Return the DSPy signature used by prompt-only dynamic agents."""

    return PromptUserAgentSignature


def _tool_user_agent_signature() -> Any:
    """Return the DSPy signature used by tool-declaring dynamic agents."""

    return ToolUserAgentSignature


__all__ = ["_prompt_user_agent_signature", "_tool_user_agent_signature"]
