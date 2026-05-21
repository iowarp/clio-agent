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
    validate_tool_items,
    validate_tool_result,
)
from clio_agent.signatures.expert_sig import DataExpertSignature
from clio_agent.tools import execution as tool_execution
from clio_agent.tools.execution import ToolExecutor, create_sync_tool_executor
from clio_agent.tools.gateway import gateway

logger = logging.getLogger(__name__)

MCPToolBridge = tool_execution.MCPToolBridge

HDF5_FILE_RESULT_FIELDS: dict[str, type | tuple[type, ...]] = {
    "filepath": str,
    "file_size_bytes": int,
    "total_datasets": int,
    "total_groups": int,
    "datasets": list,
    "groups": list,
    "compression_summary": dict,
}

HDF5_COMPRESSION_FIELDS: dict[str, type | tuple[type, ...]] = {
    "compressed_datasets": int,
    "uncompressed_datasets": int,
    "total_raw_bytes": int,
    "total_stored_bytes": int,
}

HDF5_DATASET_LIST_FIELDS: dict[str, type | tuple[type, ...]] = {
    "filepath": str,
    "total_datasets": int,
    "datasets": list,
}

HDF5_DATASET_ROW_FIELDS: dict[str, type | tuple[type, ...]] = {
    "path": str,
    "shape": list,
    "dtype": str,
    "size_bytes": int,
}

HDF5_DATASET_ANALYSIS_FIELDS: dict[str, type | tuple[type, ...]] = {
    "path": str,
    "shape": list,
    "dtype": str,
    "size_bytes": int,
    "is_chunked": bool,
}


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
            tool for tool in self._tool_executor.to_dspy_tools() if tool.name.startswith("hdf5_")
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
        if self._wants_dataset_analysis(request.question):
            return self._inspect_hdf5_dataset_request(request, str(paths[0]))
        return self._inspect_hdf5_file(request, str(paths[0]))

    def _inspect_hdf5_dataset_request(self, request: ExpertRequest, filepath: str) -> ExpertResult:
        """Run hdf5_analyze_dataset when a concrete dataset is named."""
        runner = NativeToolRunner(self._tool_executor)
        datasets = runner.call("hdf5_list_datasets", {"filepath": filepath})
        datasets_valid = validate_tool_result(
            "hdf5_list_datasets",
            datasets,
            HDF5_DATASET_LIST_FIELDS,
        )
        if not datasets_valid.ok:
            assert datasets_valid.error is not None
            runner.mark_validation_error("hdf5_list_datasets", datasets_valid.error)
            return self._hdf5_failure_result(filepath, datasets_valid.error, runner)

        datasets_data = datasets_valid.data or {}
        rows_valid = validate_tool_items(
            "hdf5_list_datasets",
            datasets_data,
            "datasets",
            HDF5_DATASET_ROW_FIELDS,
        )
        if not rows_valid.ok:
            assert rows_valid.error is not None
            runner.mark_validation_error("hdf5_list_datasets", rows_valid.error)
            return self._hdf5_failure_result(filepath, rows_valid.error, runner)

        dataset_rows = datasets_data["datasets"]
        dataset_path = self._match_dataset_path(request.question, dataset_rows)
        if not dataset_path:
            dataset_lines = [f"- {d['path']}" for d in dataset_rows[:12]]
            if len(dataset_rows) > 12:
                dataset_lines.append(f"- ... {len(dataset_rows) - 12} more datasets")
            return ExpertResult(
                analysis=(
                    f"hdf5_analyze_dataset needs a dataset path inside {filepath}. "
                    "Available datasets:\n"
                    + ("\n".join(dataset_lines) if dataset_lines else "No datasets were found.")
                ),
                recommendations=(
                    "Retry with a dataset path, for example: "
                    f"Run hdf5_analyze_dataset on {filepath} for "
                    f"{dataset_rows[0]['path'] if dataset_rows else '<dataset>'}."
                ),
                source="deterministic",
                tools=runner.observations,
                metadata={
                    "expert": "data",
                    "format": "hdf5",
                    "filepath": filepath,
                    "mode": "missing_dataset",
                },
            )

        dataset_result = runner.call(
            "hdf5_analyze_dataset",
            {"filepath": filepath, "dataset": dataset_path},
        )
        dataset_valid = validate_tool_result(
            "hdf5_analyze_dataset",
            dataset_result,
            HDF5_DATASET_ANALYSIS_FIELDS,
        )
        if not dataset_valid.ok:
            assert dataset_valid.error is not None
            runner.mark_validation_error("hdf5_analyze_dataset", dataset_valid.error)
            return self._hdf5_failure_result(filepath, dataset_valid.error, runner)

        dataset_data = dataset_valid.data or {}
        details = [
            f"- shape={dataset_data['shape']}",
            f"- dtype={dataset_data['dtype']}",
            f"- size={format_bytes(dataset_data['size_bytes'])}",
            f"- chunked={dataset_data['is_chunked']}",
        ]
        if "chunks" in dataset_data:
            details.append(f"- chunks={dataset_data['chunks']}")
        if "compression" in dataset_data:
            details.append(f"- compression={dataset_data['compression']}")
        stats = dataset_data.get("statistics")
        if isinstance(stats, dict):
            stats_bits = [
                f"{key}={stats[key]}"
                for key in ("min", "max", "mean", "sampled_elements", "total_elements")
                if key in stats
            ]
            if stats_bits:
                details.append("- statistics: " + ", ".join(stats_bits))

        return ExpertResult(
            analysis=(
                f"Analyzed HDF5 dataset {dataset_path} in {filepath}.\n" + "\n".join(details)
            ),
            recommendations=self._hdf5_recommendations(
                uncompressed=0 if dataset_data.get("compression") else 1,
                question=request.question,
            ),
            source="deterministic",
            tools=runner.observations,
            metadata={
                "expert": "data",
                "format": "hdf5",
                "filepath": filepath,
                "dataset": dataset_path,
            },
        )

    def _inspect_hdf5_file(self, request: ExpertRequest, filepath: str) -> ExpertResult:
        """Inspect a concrete HDF5 path through deterministic gateway tools."""
        runner = NativeToolRunner(self._tool_executor)
        overview = runner.call("hdf5_analyze_file", {"filepath": filepath})
        overview_valid = validate_tool_result(
            "hdf5_analyze_file",
            overview,
            HDF5_FILE_RESULT_FIELDS,
        )
        if not overview_valid.ok:
            assert overview_valid.error is not None
            runner.mark_validation_error("hdf5_analyze_file", overview_valid.error)
            return self._hdf5_failure_result(filepath, overview_valid.error, runner)

        overview_data = overview_valid.data or {}
        comp_valid = validate_tool_result(
            "hdf5_analyze_file",
            overview_data.get("compression_summary"),
            HDF5_COMPRESSION_FIELDS,
        )
        if not comp_valid.ok:
            assert comp_valid.error is not None
            runner.mark_validation_error("hdf5_analyze_file", comp_valid.error)
            return self._hdf5_failure_result(filepath, comp_valid.error, runner)

        datasets = runner.call("hdf5_list_datasets", {"filepath": filepath})
        datasets_valid = validate_tool_result(
            "hdf5_list_datasets",
            datasets,
            HDF5_DATASET_LIST_FIELDS,
        )
        if not datasets_valid.ok:
            assert datasets_valid.error is not None
            runner.mark_validation_error("hdf5_list_datasets", datasets_valid.error)
            return self._hdf5_failure_result(filepath, datasets_valid.error, runner)

        datasets_data = datasets_valid.data or {}
        rows_valid = validate_tool_items(
            "hdf5_list_datasets",
            datasets_data,
            "datasets",
            HDF5_DATASET_ROW_FIELDS,
        )
        if not rows_valid.ok:
            assert rows_valid.error is not None
            runner.mark_validation_error("hdf5_list_datasets", rows_valid.error)
            return self._hdf5_failure_result(filepath, rows_valid.error, runner)

        dataset_rows = datasets_data["datasets"]
        dataset_lines = [self._hdf5_dataset_summary_line(d) for d in dataset_rows[:12]]
        if len(dataset_rows) > 12:
            dataset_lines.append(f"- ... {len(dataset_rows) - 12} more datasets")

        comp_summary = comp_valid.data or {}
        total = overview_data["total_datasets"]
        compressed = comp_summary.get("compressed_datasets", 0)
        uncompressed = comp_summary.get("uncompressed_datasets", 0)
        ratio = comp_summary.get("overall_ratio")

        analysis = (
            f"Inspected HDF5 file {filepath}. It contains {total} datasets "
            f"and {overview_data['total_groups']} groups.\n"
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
    def _hdf5_failure_result(
        filepath: str,
        error: dict[str, Any],
        runner: NativeToolRunner,
    ) -> ExpertResult:
        return ExpertResult(
            analysis=f"Could not inspect HDF5 file {filepath}: {format_tool_error(error)}",
            recommendations="Verify the path, file readability, and HDF5 tool contract.",
            source="deterministic",
            tools=runner.observations,
            metadata={"expert": "data", "format": "hdf5", "filepath": filepath},
        )

    @staticmethod
    def _wants_dataset_analysis(question: str) -> bool:
        q_lower = question.lower()
        return any(
            token in q_lower
            for token in ("hdf5_analyze_dataset", "analyze_dataset", "analyze dataset")
        )

    @staticmethod
    def _match_dataset_path(question: str, dataset_rows: list[dict[str, Any]]) -> str | None:
        q_lower = question.lower()
        for row in dataset_rows:
            path = str(row.get("path", ""))
            if path and path.lower() in q_lower:
                return path
        return None

    @staticmethod
    def _hdf5_dataset_summary_line(row: dict[str, Any]) -> str:
        """Format a dataset row for file-level HDF5 summaries."""
        line = (
            f"- {row['path']}: shape={row['shape']}, dtype={row['dtype']}, "
            f"size={format_bytes(row['size_bytes'])}"
        )
        attrs = row.get("attributes")
        if isinstance(attrs, dict):
            unit = str(attrs.get("units") or attrs.get("unit") or "").strip()
            if unit:
                line += f", units={unit}"
        return line

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
        """Use DSPy for conceptual guidance when no file can be inspected."""
        synthesis = self.agent(
            question=request.question,
            file_context=request.file_context,
        )
        analysis = str(getattr(synthesis, "analysis", "")).strip()
        recommendations = str(getattr(synthesis, "recommendations", "")).strip()
        if not analysis:
            raise ValueError("DataExpert synthesis returned an empty analysis.")
        return ExpertResult(
            analysis=analysis,
            recommendations=recommendations,
            source="dspy",
            metadata={"expert": "data", "mode": "conceptual_synthesis"},
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
