"""
ClioAgent planner and chat signatures.

Defines the input/output interfaces for:
- AgentActionSignature: planner loop action selection over registered tools/experts
- AgentAnswerSignature: final answer synthesis from loop observations
- RouterSignature: legacy Literal router contract retained for compatibility tests
- ChatAgentSignature: conversational responses for non-data queries
"""

from typing import Literal

import dspy


class AgentActionSignature(dspy.Signature):
    """You are CLIO's agent planner.

    You control a tool-using scientific data agent. Select the next best action
    from the capabilities listed in the prompt. Use observations from previous
    steps as ground truth.

    Return exactly one JSON object and no prose. The JSON object must have one
    of these forms:

    {"action":"tool","tool":"<listed tool name>","args":{...},"reason":"..."}
    {"action":"expert","expert":"data|analysis|visualization","question":"...","reason":"..."}
    {"action":"answer","answer":"...","reason":"..."}
    {"action":"none","answer":"...","reason":"..."}

    Rules:
    - Choose only tools and experts present in capabilities.
    - Call tools when local file facts, schema, datasets, statistics, or chart
      artifacts are needed.
    - Delegate to an expert when the user asks for a higher-level task that
      matches an expert's listed tools.
    - Do not choose an expert whose listed tools/file formats cannot inspect
      the current file context.
    - Answer directly only for conversation, capability questions, or after
      observations are sufficient.
    - Never invent file-specific facts. Use only observations for file facts.
    - If a tool failed, answer with the failure and the next concrete action
      instead of pretending the file was inspected.
    """

    question: str = dspy.InputField(desc="User's current message")
    session_context: str = dspy.InputField(desc="Relevant conversation history")
    file_context: str = dspy.InputField(desc="Current file context, if any")
    capabilities: str = dspy.InputField(desc="Registered experts and callable tools")
    observations: str = dspy.InputField(desc="Prior loop observations for this request")
    action_json: str = dspy.OutputField(desc="One JSON action object")


class AgentAnswerSignature(dspy.Signature):
    """You are CLIO answering after executing agent-loop actions.

    Use the observations as ground truth. Do not invent local file contents,
    schemas, datasets, statistics, or artifact paths that are not in the
    observations. If the observations contain an error, explain the error and
    the next useful action.
    """

    question: str = dspy.InputField(desc="User's current message")
    session_context: str = dspy.InputField(desc="Relevant conversation history")
    observations: str = dspy.InputField(desc="Tool/expert observations from this request")
    answer: str = dspy.OutputField(desc="Final user-facing answer")


class RouterSignature(dspy.Signature):
    """Legacy typed route contract retained for compatibility.

    Production CLIO uses AgentActionSignature for the tool/expert loop. This
    older signature remains importable for integrations that still expect a
    Literal selected_expert output covering chat, data, analysis, visualization,
    and none.
    """

    question: str = dspy.InputField(desc="User's question or message")
    selected_expert: Literal["chat", "data", "analysis", "visualization", "none"] = dspy.OutputField(
        desc="Legacy route id: chat, data, analysis, visualization, or none"
    )


class ChatAgentSignature(dspy.Signature):
    """You are CLIO, an autonomous science agent for scientific data management.
    You are having a conversation with a scientist or researcher.

    Identity: You are CLIO (the agent). The system you run in is the CLIO Framework.
    You help with scientific data management: HDF5 optimization, Parquet analysis,
    statistical profiling, and data visualization.

    For identity questions: Introduce yourself as CLIO and describe your capabilities.
    For general questions: Be helpful, precise, and suggest how your data expertise
    could help if relevant. Mention available experts: DataExpert for HDF5 analysis,
    AnalysisExpert for Parquet/statistical profiling, VisualizationExpert for charts.

    Do not invent file-specific facts from conversation history. If the user asks
    for details about a local file, dataset, schema, columns, statistics, or plots,
    the answer must come from the routed expert/tool path, not chat synthesis.

    Keep responses concise but informative. Be confident and direct."""

    question: str = dspy.InputField(desc="User's question or message")
    session_context: str = dspy.InputField(
        desc="Relevant context from conversation history"
    )
    answer: str = dspy.OutputField(desc="CLIO's conversational response")
