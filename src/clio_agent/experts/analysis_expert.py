"""
ClioAgent Analysis Expert Module

Specializes in statistical analysis and data profiling of tabular
datasets (Parquet). Uses ReAct with real Parquet MCP tools from the
CLIO gateway via MCPToolBridge for tool-backed analysis.

The AnalysisExpert connects to the FastMCP gateway, filters to
Parquet-prefixed tools, and uses DSPy ReAct to reason and act.

Example:
    >>> from clio_agent.experts import AnalysisExpert
    >>> from clio_agent.config import setup_dspy
    >>>
    >>> lm = setup_dspy()
    >>> expert = AnalysisExpert()
    >>> result = expert(
    ...     question="What are the statistics for the temperature column?",
    ...     file_context="data.parquet, 100 rows, weather sensor data"
    ... )
    >>> print(result.analysis)
    >>> print(result.recommendations)
"""

import logging
from typing import Any, Optional

import dspy

from clio_agent.signatures.analysis_sig import AnalysisExpertSignature
from clio_agent.tools.execution import MCPToolBridge, ToolExecutor
from clio_agent.tools.gateway import gateway

logger = logging.getLogger(__name__)


_PARALLEL_TRIGGERS = (
    "validate ",
    "check ",
    "analyze ",
    "compare ",
    "profile ",
)


def _detect_parallel_items(question: str) -> list[str]:
    """Pull comma/and-separated items out of a "validate X, Y, and Z"
    style question. Empty list when the question doesn't match a
    parallel trigger or has only one item — no spawning in that case.

    Heuristic, not perfect — the goal is to surface obvious fan-out
    patterns. Tier-2 experts opt in by checking the result.
    """

    q = question.lower().strip()
    trigger = next((t for t in _PARALLEL_TRIGGERS if t in q), None)
    if trigger is None:
        return []
    after = q.split(trigger, 1)[1]
    # Split on " and " then commas.
    parts: list[str] = []
    for chunk in after.split(" and "):
        for piece in chunk.split(","):
            piece = piece.strip().strip(".")
            if piece:
                parts.append(piece)
    if len(parts) < 2:
        return []
    return parts


class AnalysisExpert(dspy.Module):
    """Statistical analysis expert with ReAct + real Parquet MCP tools.

    Connects to the CLIO MCP gateway via MCPToolBridge to load Parquet
    analysis tools, then uses DSPy ReAct for tool-augmented reasoning
    about data content, distributions, and quality.

    Attributes:
        arc_memory: Optional ARC memory instance for caching
        agent: DSPy ReAct module with Parquet MCP tools

    Example:
        >>> expert = AnalysisExpert()
        >>> print(f"Loaded {len(expert._tools)} tools")
        >>> result = expert(
        ...     question="Analyze the schema of data.parquet",
        ...     file_context="/path/to/data.parquet, weather sensor data"
        ... )
        >>> print(result.analysis)
    """

    def __init__(
        self,
        arc_memory: Optional[Any] = None,
        tool_executor: Optional[ToolExecutor] = None,
    ):
        """Initialize Analysis Expert with ReAct and Parquet MCP tools.

        Args:
            arc_memory: Optional ARCMemory instance for tool result caching
            tool_executor: Optional executor for MCP-backed tools
        """
        super().__init__()
        self.arc_memory = arc_memory

        # Bridge MCP tools to DSPy tools via an injectable execution boundary.
        self._bridge = tool_executor or MCPToolBridge(gateway)
        all_tools = self._bridge.to_dspy_tools()

        # Filter to only parquet-prefixed tools
        self._tools = [t for t in all_tools if t.name.startswith("parquet_")]

        logger.info(
            "AnalysisExpert initialized with %d tools: %s",
            len(self._tools),
            [t.name for t in self._tools],
        )

        # ReAct agent with Parquet MCP-backed tools
        self.agent = dspy.ReAct(
            AnalysisExpertSignature,
            tools=self._tools,
            max_iters=5,
        )

    def forward(self, question: str, file_context: str = "") -> dspy.Prediction:
        """Generate statistical analysis using ReAct with Parquet MCP tools.

        Args:
            question: User's question about data analysis or statistics
            file_context: File information (paths, column names, context)

        Returns:
            dspy.Prediction with analysis and recommendations fields.
            iowarp/clio-agent#9: when the question matches a parallel
            pattern ("validate X and Y" / "check X, Y, and Z"), spawn
            one Tier-3 nanoagent per item via
            ``clio_agent.runtime.nanoagent.spawn_many`` and attach
            the results to ``Prediction.nanoagents_spawned``. The
            GACT layer materialises them as child sessions.
        """

        nanoagents_spawned: list[dict[str, Any]] = []
        items = _detect_parallel_items(question)
        if items:
            from clio_agent.runtime.nanoagent import spawn_many

            spawns = spawn_many(
                agent_factory=lambda: self.agent,
                items=[
                    {
                        "agent_id": "analysis_validator",
                        "input": {"question": f"Validate: {item}"},
                    }
                    for item in items
                ],
                question_field="question",
                num_threads=min(4, len(items)),
            )
            nanoagents_spawned = [s.to_wire() for s in spawns]

        # iowarp/clio-agent#9: spawns are valuable signal even when
        # the main ReAct call fails — surface them on a stub
        # Prediction so the GACT layer still publishes
        # subagent.started/completed and renders the child sessions.
        try:
            result = self.agent(question=question, file_context=file_context)
        except Exception as exc:  # noqa: BLE001
            if nanoagents_spawned:
                result = dspy.Prediction(
                    analysis=(
                        f"Spawned {len(nanoagents_spawned)} nanoagent(s) "
                        f"to {', '.join(s['input'].get('question', '') for s in nanoagents_spawned)[:200]}. "
                        f"The aggregator step failed ({type(exc).__name__}); "
                        "see child sessions for individual results."
                    ),
                    recommendations="Inspect each child session for the per-item finding.",
                )
            else:
                raise
        if nanoagents_spawned:
            # dspy.Prediction supports attribute set via __setattr__.
            try:
                result.nanoagents_spawned = nanoagents_spawned  # type: ignore[attr-defined]
            except Exception:
                pass
        return result

    def close(self) -> None:
        """Release tool execution resources."""
        self._bridge.close()

    @staticmethod
    def get_capabilities() -> dict[str, Any]:
        """Return expert capabilities for agent routing.

        Returns:
            Dictionary with name, description, keywords, priority.
            Used by ClioAgent to route questions to this expert.
        """
        return {
            "name": "Analysis Expert",
            "description": (
                "Specializes in statistical analysis, data profiling, and quality "
                "assessment of tabular datasets (Parquet). Computes column-level "
                "statistics, identifies distributions, and flags data quality issues."
            ),
            "keywords": [
                "parquet",
                "statistics",
                "analysis",
                "schema",
                "distribution",
                "data quality",
                "columnar",
                "profiling",
                "null count",
                "outliers",
            ],
            "priority": 2,
        }
