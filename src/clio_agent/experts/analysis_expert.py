"""
ClioAgent Analysis Expert Module

Specializes in statistical analysis and data profiling of tabular
datasets. Uses deterministic CLIO tool execution first and DSPy only for
optional non-file synthesis.

The AnalysisExpert connects to the FastMCP gateway, filters to Parquet tools,
runs them directly for explicit Parquet file questions, and records tool
provenance for ARC traces. CSV inspection remains a native local fallback.

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
import time
from typing import Any, Optional

import dspy

from clio_agent.experts.native_tools import NativeToolRunner
from clio_agent.harness import (
    ExpertRequest,
    ExpertResult,
    ToolObservation,
    extract_file_paths,
    format_tool_error,
    tool_result_ok,
)
from clio_agent.signatures.analysis_sig import AnalysisExpertSignature
from clio_agent.tools.execution import ToolExecutor, create_sync_tool_executor
from clio_agent.tools.file_policy import FilePolicyError, validate_read_path
from clio_agent.tools.gateway import gateway

logger = logging.getLogger(__name__)


class AnalysisExpert(dspy.Module):
    """Statistical analysis expert with native Parquet and CSV execution.

    Connects to the CLIO MCP gateway via a sync tool executor, executes
    deterministic Parquet tools for explicit file requests, and falls back to
    DSPy synthesis only when no file can be inspected.

    Attributes:
        arc_memory: Optional ARC memory instance for caching
        agent: DSPy synthesis module for optional non-file responses

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
        """Initialize Analysis Expert with native tools and optional synthesis.

        Args:
            arc_memory: Optional ARCMemory instance for tool result caching
            tool_executor: Optional sync executor for MCP-backed tools
        """
        super().__init__()
        self.arc_memory = arc_memory

        self._tool_executor = tool_executor or create_sync_tool_executor(gateway)
        self._bridge = self._tool_executor
        all_tools = self._tool_executor.to_dspy_tools()

        # Filter to only parquet-prefixed tools
        self._tools = [t for t in all_tools if t.name.startswith("parquet_")]

        logger.info(
            "AnalysisExpert initialized with %d tools: %s",
            len(self._tools),
            [t.name for t in self._tools],
        )

        self.agent = dspy.Predict(AnalysisExpertSignature)

    def forward(self, question: str, file_context: str = "") -> dspy.Prediction:
        """Generate statistical analysis using the native CLIO expert contract.

        Args:
            question: User's question about data analysis or statistics
            file_context: File information (paths, column names, context)

        Returns:
            dspy.Prediction with analysis and recommendations fields
        """
        result = self.run(ExpertRequest(question=question, file_context=file_context))
        return self._to_prediction(result)

    def run(self, request: ExpertRequest) -> ExpertResult:
        """Run the typed native expert boundary."""
        parquet_paths = extract_file_paths(request.question, request.file_context, {".parquet"})
        if parquet_paths:
            return self._inspect_parquet_file(request, str(parquet_paths[0]))

        csv_paths = extract_file_paths(request.question, request.file_context, {".csv"})
        if csv_paths:
            return self._inspect_csv_file(str(csv_paths[0]))

        return self._synthesize_without_tools(request)

    def _inspect_parquet_file(self, request: ExpertRequest, filepath: str) -> ExpertResult:
        """Inspect a concrete Parquet path through deterministic gateway tools."""
        runner = NativeToolRunner(self._tool_executor)
        schema = runner.call("parquet_analyze_schema", {"filepath": filepath})
        if isinstance(schema, dict) and "error" in schema:
            return ExpertResult(
                analysis=(
                    f"Could not inspect Parquet file {filepath}: "
                    f"{format_tool_error(schema['error'])}"
                ),
                recommendations="Verify the path exists and that the file is readable Parquet.",
                source="deterministic",
                tools=runner.observations,
                metadata={"expert": "analysis", "format": "parquet", "filepath": filepath},
            )

        if not isinstance(schema, dict):
            return ExpertResult(
                analysis=f"Could not inspect Parquet file {filepath}: unexpected tool result.",
                recommendations="Retry the inspection and check the gateway health if it repeats.",
                source="deterministic",
                tools=runner.observations,
                metadata={"expert": "analysis", "format": "parquet", "filepath": filepath},
            )

        columns = schema.get("columns", [])
        column_lines = [
            f"- {c['name']}: {c['type']}, nullable={c['nullable']}" for c in columns[:12]
        ]
        if len(columns) > 12:
            column_lines.append(f"- ... {len(columns) - 12} more columns")

        stats_lines = []
        for name in self._select_stat_columns(request.question, columns):
            stats = runner.call(
                "parquet_compute_statistics",
                {"filepath": filepath, "column": name},
            )
            if isinstance(stats, dict) and "error" not in stats:
                stats_bits = [
                    f"{k}={stats[k]}"
                    for k in ("min", "max", "mean", "median", "std", "null_count", "unique_count")
                    if k in stats
                ]
                stats_lines.append(f"{name}: " + ", ".join(stats_bits))

        analysis = (
            f"Inspected Parquet file {filepath}. It has {schema.get('num_rows')} rows, "
            f"{schema.get('num_columns')} columns, and {schema.get('num_row_groups')} row groups.\n"
            + ("\n".join(column_lines) if column_lines else "No columns were found.")
        )
        if stats_lines:
            analysis += "\n\nColumn statistics:\n" + "\n".join(stats_lines)

        recommendations = (
            "Use the schema and row group count to decide whether the file needs repartitioning. "
            "For analysis questions, compute statistics on the specific columns involved instead "
            "of scanning every column."
        )

        return ExpertResult(
            analysis=analysis,
            recommendations=recommendations,
            source="deterministic",
            tools=runner.observations,
            metadata={"expert": "analysis", "format": "parquet", "filepath": filepath},
        )

    @staticmethod
    def _select_stat_columns(question: str, columns: list[dict[str, Any]]) -> list[str]:
        q_lower = question.lower()
        wants_stats = any(token in q_lower for token in ("stat", "profile", "quality", "null"))
        named: list[str] = []
        for col in columns:
            name = str(col.get("name", ""))
            if not name:
                continue
            if name.lower() in q_lower:
                named.append(name)
            if len(named) >= 4:
                break
        if named:
            return named
        if wants_stats:
            return [str(col["name"]) for col in columns[:4] if col.get("name")]
        return []

    def _inspect_csv_file(self, filepath: str) -> ExpertResult:
        """Inspect a concrete CSV path with native pyarrow execution."""
        start = time.time()
        try:
            import pyarrow.csv as pcsv

            safe_path = validate_read_path(filepath)
            table = pcsv.read_csv(safe_path)
            tool_result: dict[str, Any] = {
                "rows": table.num_rows,
                "columns": table.num_columns,
                "filepath": str(safe_path),
            }
        except FilePolicyError as exc:
            result = exc.to_result()
            return ExpertResult(
                analysis=(
                    f"Could not inspect CSV file {filepath}: "
                    f"{format_tool_error(result['error'])}"
                ),
                recommendations="Move the file under an allowed root or adjust CLIO_ALLOWED_ROOTS.",
                source="deterministic",
                tools=(
                    ToolObservation(
                        tool="csv_read_table",
                        params={"filepath": filepath},
                        result=result,
                        duration_ms=(time.time() - start) * 1000,
                        ok=False,
                    ),
                ),
                metadata={"expert": "analysis", "format": "csv", "filepath": filepath},
            )
        except Exception as exc:
            result = {"error": str(exc)}
            return ExpertResult(
                analysis=f"Could not inspect CSV file {filepath}: {exc}",
                recommendations="Verify the path exists and that the file is readable CSV.",
                source="deterministic",
                tools=(
                    ToolObservation(
                        tool="csv_read_table",
                        params={"filepath": filepath},
                        result=result,
                        duration_ms=(time.time() - start) * 1000,
                        ok=False,
                    ),
                ),
                metadata={"expert": "analysis", "format": "csv", "filepath": filepath},
            )

        observation = ToolObservation(
            tool="csv_read_table",
            params={"filepath": filepath},
            result=tool_result,
            duration_ms=(time.time() - start) * 1000,
            ok=tool_result_ok(tool_result),
        )

        column_lines = []
        for field in table.schema:
            null_count = table.column(field.name).null_count
            column_lines.append(f"- {field.name}: {field.type}, nulls={null_count}")

        analysis = (
            f"Inspected CSV file {safe_path}. It has {table.num_rows} rows and "
            f"{table.num_columns} columns.\n"
            + ("\n".join(column_lines) if column_lines else "No columns were found.")
        )
        recommendations = (
            "CSV is readable in local mode. For repeated analysis or larger files, convert to "
            "Parquet so schema, compression, and column statistics are cheaper to inspect."
        )

        return ExpertResult(
            analysis=analysis,
            recommendations=recommendations,
            source="deterministic",
            tools=(observation,),
            metadata={"expert": "analysis", "format": "csv", "filepath": filepath},
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
                    metadata={"expert": "analysis", "mode": "conceptual_synthesis"},
                )
        except Exception as exc:
            logger.debug("AnalysisExpert synthesis fallback failed: %s", exc)

        return ExpertResult(
            analysis=(
                "No concrete Parquet or CSV file path was available to inspect. I can discuss "
                "analysis strategy, but file-specific statistics require tool results."
            ),
            recommendations=(
                "Provide a Parquet or CSV file path for inspection. For data profiling, start "
                "with schema discovery, then compute statistics only for the columns needed by "
                "the question."
            ),
            source="fallback",
            metadata={"expert": "analysis", "mode": "no_file"},
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
