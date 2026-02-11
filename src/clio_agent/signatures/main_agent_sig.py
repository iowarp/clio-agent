"""
ClioAgent Router and Chat Agent Signatures

Defines the input/output interfaces for:
- RouterSignature: Lightweight routing with Literal typed output
- ChatAgentSignature: Conversational responses for non-data queries
"""

from typing import Literal

import dspy


class RouterSignature(dspy.Signature):
    """You are the CLIO Router. Your only job is to classify user intent and
    route to the correct handler. You do NOT answer questions yourself.

    Route to "data" when the user asks about:
    - HDF5 files, datasets, compression, chunking
    - Data format optimization, I/O performance
    - File analysis, storage optimization
    - Any question mentioning specific file paths or data files

    Route to "chat" when the user:
    - Greets you (hello, hi, hey)
    - Asks who you are or what you can do
    - Makes general conversation not about data files
    - Asks about CLIO itself

    When in doubt, route to "chat" -- it's better to have a conversation
    than to invoke an expert unnecessarily."""

    question: str = dspy.InputField(desc="User's question or message")
    selected_expert: Literal["data", "chat"] = dspy.OutputField(
        desc="Route to 'data' for file/IO questions, 'chat' for everything else"
    )


class ChatAgentSignature(dspy.Signature):
    """You are CLIO, an autonomous science agent for scientific data management.
    You are having a conversation with a scientist or researcher.

    Identity: You are CLIO (the agent). The system you run in is the CLIO Framework.
    You help with HDF5 file optimization, compression, chunking, and I/O performance.

    For identity questions: Introduce yourself as CLIO and describe your capabilities.
    For general questions: Be helpful, precise, and suggest how your data expertise
    could help if relevant. Mention available experts (DataExpert for HDF5 analysis).

    Keep responses concise but informative. Be confident and direct."""

    question: str = dspy.InputField(desc="User's question or message")
    session_context: str = dspy.InputField(
        desc="Relevant context from conversation history"
    )
    answer: str = dspy.OutputField(desc="CLIO's conversational response")
