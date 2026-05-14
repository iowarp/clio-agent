"""
ClioAgent Signatures Module

DSPy signature definitions for ClioAgent components.

Available Signatures:
- AgentActionSignature: Selects loop actions over registered experts/tools
- AgentAnswerSignature: Synthesizes answers from loop observations
- RouterSignature: Legacy Literal router contract
- ChatAgentSignature: Conversational responses for non-data queries
- DataExpertSignature: HDF5 and data-layout synthesis
- AnalysisExpertSignature: Statistical analysis and data profiling synthesis
- VisualizationExpertSignature: Scientific data visualization

Example:
    >>> import dspy
    >>> from clio_agent.signatures import RouterSignature, ChatAgentSignature
    >>>
    >>> router = dspy.ChainOfThought(RouterSignature)
    >>> chat = dspy.ChainOfThought(ChatAgentSignature)
"""

from clio_agent.signatures.analysis_sig import (
    AnalysisExpertSignature,
)
from clio_agent.signatures.expert_sig import (
    DataExpertSignature,
)
from clio_agent.signatures.main_agent_sig import (
    AgentActionSignature,
    AgentAnswerSignature,
    ChatAgentSignature,
    RouterSignature,
)
from clio_agent.signatures.visualization_sig import (
    VisualizationExpertSignature,
)

__all__ = [
    "AgentActionSignature",
    "AgentAnswerSignature",
    "RouterSignature",
    "ChatAgentSignature",
    "DataExpertSignature",
    "AnalysisExpertSignature",
    "VisualizationExpertSignature",
]
