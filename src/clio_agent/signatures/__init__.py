"""
ClioAgent Signatures Module

DSPy signature definitions for ClioAgent components.

Available Signatures:
- RouterSignature: Routes questions to experts via Literal typed output
- ChatAgentSignature: Conversational responses for non-data queries
- DataExpertSignature: HDF5, Parquet optimization with ReAct tools

Example:
    >>> import dspy
    >>> from clio_agent.signatures import RouterSignature, ChatAgentSignature
    >>>
    >>> router = dspy.ChainOfThought(RouterSignature)
    >>> chat = dspy.ChainOfThought(ChatAgentSignature)
"""

from clio_agent.signatures.expert_sig import (
    DataExpertSignature,
)
from clio_agent.signatures.main_agent_sig import (
    ChatAgentSignature,
    RouterSignature,
)

__all__ = [
    "RouterSignature",
    "ChatAgentSignature",
    "DataExpertSignature",
]
