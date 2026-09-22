"""DSPy signatures shared by registered prompt and tool agents."""

from __future__ import annotations

from typing import Any

_PROMPT_SIGNATURE: Any = None
_TOOL_SIGNATURE: Any = None


def _build_signature(name: str) -> Any:
    """Build one dynamic-agent signature when a real model turn needs it."""

    import dspy  # noqa: PLC0415

    namespace = {
        "__doc__": f"Run a registered CLIO {name} agent using runtime instructions.",
        "__annotations__": {
            "system_prompt": str,
            "question": str,
            "images": list[dspy.Image],
            "files": list[dspy.File],
            "answer": str,
            "expert_handoffs": str,
        },
        "system_prompt": dspy.InputField(desc="Registered agent instructions"),
        "question": dspy.InputField(desc="User message for this agent"),
        "images": dspy.InputField(desc="User-provided images for this turn"),
        "files": dspy.InputField(desc="User-provided PDF documents for this turn"),
        "answer": dspy.OutputField(desc="User-facing answer"),
        "expert_handoffs": dspy.OutputField(
            desc=(
                "JSON array of synchronous child expert delegations to execute next. "
                "Use [] when no child expert should be called."
            )
        ),
    }
    return type(f"{name.title()}UserAgentSignature", (dspy.Signature,), namespace)


def _prompt_user_agent_signature() -> Any:
    """Return the DSPy signature used by prompt-only dynamic agents."""

    global _PROMPT_SIGNATURE  # noqa: PLW0603
    if _PROMPT_SIGNATURE is None:
        _PROMPT_SIGNATURE = _build_signature("prompt")
    return _PROMPT_SIGNATURE


def _tool_user_agent_signature() -> Any:
    """Return the DSPy signature used by tool-declaring dynamic agents."""

    global _TOOL_SIGNATURE  # noqa: PLW0603
    if _TOOL_SIGNATURE is None:
        _TOOL_SIGNATURE = _build_signature("tool")
    return _TOOL_SIGNATURE


__all__ = ["_prompt_user_agent_signature", "_tool_user_agent_signature"]
