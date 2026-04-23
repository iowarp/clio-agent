"""
ClioAgent Data Expert Module

Specializes in scientific data file optimization (HDF5, Parquet).
Uses deterministic CLIO tool execution first and DSPy only for optional
non-file synthesis.

The DataExpert connects to the FastMCP gateway through the CLIO tool execution
boundary, runs HDF5 tools directly for explicit file questions, and returns
typed CLIO results with tool provenance for ARC traces.

Example:
    >>> from clio_agent.experts import DataExpert
    >>> from clio_agent.config import setup_dspy
    >>>
    >>> lm = setup_dspy()
    >>> expert = DataExpert()
    >>> result = expert(
    ...     question="How do I optimize HDF5 compression for my 100GB simulation output?",
    ...     file_context="Using parallel HDF5 on 64 cores, mostly float64 data"
    ... )
    >>> print(result.analysis)
    >>> print(result.recommendations)
"""

import logging
from typing import Any, Optional

import dspy

from clio_agent.experts.native_tools import NativeToolRunner
from clio_agent.harness import (
    ExpertRequest,
    ExpertResult,
    extract_file_paths,
    format_bytes,
    format_tool_error,
)
from clio_agent.signatures.expert_sig import DataExpertSignature
from clio_agent.tools import execution as tool_execution
from clio_agent.tools.execution import ToolExecutor, create_sync_tool_executor
from clio_agent.tools.gateway import gateway

logger = logging.getLogger(__name__)

MCPToolBridge = tool_execution.MCPToolBridge


class DataExpert(dspy.Module):
    """Scientific data expert with native HDF5 tool execution.

    Connects to the CLIO MCP gateway through a sync tool executor, executes
    deterministic HDF5 tools for explicit file requests, and falls back to DSPy
    synthesis only for conceptual questions where no file can be inspected.

    Attributes:
        arc_memory: Optional ARC memory instance for caching
        agent: DSPy synthesis module for optional non-file responses

    Example:
        >>> expert = DataExpert()
        >>> print(f"Loaded {len(expert._tools)} tools")
        >>> result = expert(
        ...     question="Analyze compression in my_data.h5",
        ...     file_context="/path/to/my_data.h5, 2GB, climate simulation"
        ... )
        >>> print(result.analysis)
    """

    def __init__(
        self,
        arc_memory: Optional[Any] = None,
        tool_executor: Optional[ToolExecutor] = None,
    ):
        """Initialize Data Expert with native tools and optional synthesis.

        Args:
            arc_memory: Optional ARCMemory instance for tool result caching
            tool_executor: Optional sync executor for MCP-backed tools
        """
        super().__init__()
        self.arc_memory = arc_memory

        self._tool_executor = tool_executor or create_sync_tool_executor(gateway)
        self._bridge = self._tool_executor
        self._tools = [
            tool
            for tool in self._tool_executor.to_dspy_tools()
            if tool.name.startswith("hdf5_")
        ]

        logger.info(
            "DataExpert initialized with %d tools: %s",
            len(self._tools),
            [t.name for t in self._tools],
        )

        self.agent = dspy.Predict(DataExpertSignature)

    def forward(self, question: str, file_context: str = "") -> dspy.Prediction:
        """Generate data I/O analysis using the native CLIO expert contract.

        Args:
            question: User's question about data files or I/O optimization
            file_context: File information (paths, sizes, formats)

        Returns:
            dspy.Prediction with analysis and recommendations fields
        """
        result = self.run(ExpertRequest(question=question, file_context=file_context))
        return self._to_prediction(result)

    def run(self, request: ExpertRequest) -> ExpertResult:
        """Run the typed native expert boundary."""
        paths = extract_file_paths(request.question, request.file_context, {".h5", ".hdf5"})
        if not paths:
            return self._synthesize_without_tools(request)
        return self._inspect_hdf5_file(request, str(paths[0]))

    def _inspect_hdf5_file(self, request: ExpertRequest, filepath: str) -> ExpertResult:
        """Inspect a concrete HDF5 path through deterministic gateway tools."""
        runner = NativeToolRunner(self._tool_executor)
        overview = runner.call("hdf5_analyze_file", {"filepath": filepath})
        datasets = runner.call("hdf5_list_datasets", {"filepath": filepath})

        if isinstance(overview, dict) and "error" in overview:
            return ExpertResult(
                analysis=(
                    f"Could not inspect HDF5 file {filepath}: "
                    f"{format_tool_error(overview['error'])}"
                ),
                recommendations="Verify the path exists and that the file is readable HDF5.",
                source="deterministic",
                tools=runner.observations,
                metadata={"expert": "data", "format": "hdf5", "filepath": filepath},
            )

        if not isinstance(overview, dict):
            return ExpertResult(
                analysis=f"Could not inspect HDF5 file {filepath}: unexpected tool result.",
                recommendations="Retry the inspection and check the gateway health if it repeats.",
                source="deterministic",
                tools=runner.observations,
                metadata={"expert": "data", "format": "hdf5", "filepath": filepath},
            )

        dataset_rows = datasets.get("datasets", []) if isinstance(datasets, dict) else []
        dataset_lines = [
            f"- {d['path']}: shape={d['shape']}, dtype={d['dtype']}, "
            f"size={format_bytes(d['size_bytes'])}"
            for d in dataset_rows[:12]
        ]
        if len(dataset_rows) > 12:
            dataset_lines.append(f"- ... {len(dataset_rows) - 12} more datasets")

        comp_summary = overview.get("compression_summary", {})
        total = overview.get("total_datasets", len(dataset_rows))
        compressed = comp_summary.get("compressed_datasets", 0)
        uncompressed = comp_summary.get("uncompressed_datasets", 0)
        ratio = comp_summary.get("overall_ratio")

        analysis = (
            f"Inspected HDF5 file {filepath}. It contains {total} datasets "
            f"and {overview.get('total_groups', 0)} groups.\n"
            + ("\n".join(dataset_lines) if dataset_lines else "No datasets were found.")
            + "\n\n"
            f"Compression summary: {compressed} compressed, {uncompressed} uncompressed."
        )
        if ratio is not None:
            analysis += f" Overall raw-to-stored ratio is about {ratio}x."

        recommendations = self._hdf5_recommendations(
            uncompressed=int(uncompressed or 0),
            question=request.question,
        )

        return ExpertResult(
            analysis=analysis,
            recommendations=recommendations,
            source="deterministic",
            tools=runner.observations,
            metadata={"expert": "data", "format": "hdf5", "filepath": filepath},
        )

    @staticmethod
    def _hdf5_recommendations(*, uncompressed: int, question: str) -> str:
        q_lower = question.lower()
        if "chunk" in q_lower:
            return (
                "Use the dataset shapes above to pick a concrete dataset, then run chunk "
                "optimization for its dominant access pattern. Keep chunk payloads near "
                "1 MiB as the first target and avoid contiguous layout for datasets that "
                "need compression or partial reads."
            )
        if uncompressed:
            return (
                "Compression is partially configured. Review uncompressed numeric datasets and "
                "consider chunked gzip or lzf compression when read patterns tolerate it. Keep "
                "chunk sizes near 1 MiB as a starting point, then tune for row, column, or "
                "random access."
            )
        return (
            "Compression coverage looks reasonable. Validate chunk shapes against the dominant "
            "read pattern before changing the file layout."
        )

    def _synthesize_without_tools(self, request: ExpertRequest) -> ExpertResult:
        """Use DSPy only for conceptual guidance when no file can be inspected."""
        try:
            synthesis = self.agent(
                question=request.question,
                file_context=request.file_context,
            )
            analysis = str(getattr(synthesis, "analysis", "")).strip()
            recommendations = str(getattr(synthesis, "recommendations", "")).strip()
            if analysis:
                return ExpertResult(
                    analysis=analysis,
                    recommendations=recommendations,
                    source="dspy",
                    metadata={"expert": "data", "mode": "conceptual_synthesis"},
                )
        except Exception as exc:
            logger.debug("DataExpert synthesis fallback failed: %s", exc)

        return ExpertResult(
            analysis=(
                "No concrete HDF5 file path was available to inspect. I can give general "
                "HDF5 layout guidance, but file-specific conclusions require tool results."
            ),
            recommendations=(
                "Provide an HDF5 file path for inspection. For general tuning, start by "
                "checking dataset shapes, compression coverage, chunk payload size, and the "
                "dominant read pattern before rewriting the file."
            ),
            source="fallback",
            metadata={"expert": "data", "mode": "no_file"},
        )

    @staticmethod
    def _to_prediction(result: ExpertResult) -> dspy.Prediction:
        return dspy.Prediction(
            analysis=result.analysis,
            recommendations=result.recommendations,
            synthesis_source=result.source,
            tool_provenance=list(result.tools),
            metadata=dict(result.metadata),
        )

    def close(self) -> None:
        """Release tool execution resources."""
        self._tool_executor.close()

    @staticmethod
    def get_capabilities() -> dict[str, Any]:
        """Return expert capabilities for agent routing.

        Returns:
            Dictionary with name, description, keywords, priority.
            Used by ClioAgent to route questions to this expert.
        """
        return {
            "name": "Data Expert",
            "description": (
                "Specializes in scientific data file optimization (HDF5, Parquet), "
                "compression strategies, I/O performance, and format conversion"
            ),
            "keywords": [
                "hdf5",
                "parquet",
                "compression",
                "chunking",
                "data format",
                "file optimization",
                "i/o performance",
                "parallel io",
                "mpi-io",
            ],
            "priority": 1,
        }
