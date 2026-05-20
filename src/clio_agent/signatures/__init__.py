"""
ClioAgent Signatures Module

DSPy signature definitions for ClioAgent components.

Available Signatures:
- AgentActionSignature: Selects loop actions over registered experts/tools
- AgentAnswerSignature: Synthesizes answers from loop observations
- ChatAgentSignature: Conversational responses for non-data queries
- DataExpertSignature: HDF5 and data-layout synthesis
- AnalysisExpertSignature: Statistical analysis and data profiling synthesis
- VisualizationExpertSignature: Scientific data visualization

Example:
    >>> import dspy
    >>> from clio_agent.signatures import AgentActionSignature, ChatAgentSignature
    >>>
    >>> planner = dspy.Predict(AgentActionSignature)
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
)
from clio_agent.signatures.visualization_sig import (
    VisualizationExpertSignature,
)

__all__ = [
    "AgentActionSignature",
    "AgentAnswerSignature",
    "ChatAgentSignature",
    "DataExpertSignature",
    "AnalysisExpertSignature",
    "VisualizationExpertSignature",
]
