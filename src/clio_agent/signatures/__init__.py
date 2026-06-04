"""
ClioAgent Signatures Module

DSPy signature definitions for ClioAgent components.

Available Signatures:
- AgentActionSignature: Selects loop actions over registered experts/tools
- AgentAnswerSignature: Synthesizes answers from loop observations
- ChatAgentSignature: Conversational responses for non-data queries

Example:
    >>> import dspy
    >>> from clio_agent.signatures import AgentActionSignature, ChatAgentSignature
    >>>
    >>> planner = dspy.Predict(AgentActionSignature)
    >>> chat = dspy.ChainOfThought(ChatAgentSignature)
"""

from clio_agent.signatures.main_agent_sig import (
    AgentActionSignature,
    AgentAnswerSignature,
    ChatAgentSignature,
)

__all__ = [
    "AgentActionSignature",
    "AgentAnswerSignature",
    "ChatAgentSignature",
]
