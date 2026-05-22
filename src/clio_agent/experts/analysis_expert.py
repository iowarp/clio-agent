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
    normalize_tool_error,
    normalize_tool_result,
    tool_result_ok,
    validate_tool_items,
    validate_tool_result,
)
from clio_agent.signatures.analysis_sig import AnalysisExpertSignature
from clio_agent.tools.execution import ToolExecutor, create_sync_tool_executor
from clio_agent.tools.file_policy import FilePolicyError, validate_read_path
from clio_agent.tools.gateway import gateway

logger = logging.getLogger(__name__)

PARQUET_SCHEMA_FIELDS: dict[str, type | tuple[type, ...]] = {
    "filepath": str,
    "num_columns": int,
    "columns": list,
    "num_rows": int,
    "num_row_groups": int,
    "file_size_bytes": int,
}

PARQUET_COLUMN_FIELDS: dict[str, type | tuple[type, ...]] = {
    "name": str,
    "type": str,
    "nullable": bool,
}

PARQUET_STATS_FIELDS: dict[str, type | tuple[type, ...]] = {
    "filepath": str,
    "column": str,
    "dtype": str,
    "total_count": int,
    "null_count": int,
    "unique_count": int,
}

CSV_RESULT_FIELDS: dict[str, type | tuple[type, ...]] = {
    "filepath": str,
    "rows": int,
    "columns": int,
    "schema": list,
}

CSV_COLUMN_FIELDS: dict[str, type | tuple[type, ...]] = {
    "name": str,
    "type": str,
    "null_count": int,
}

NDP_ORGANIZATION_FIELDS: dict[str, type | tuple[type, ...]] = {
    "organizations": list,
    "count": int,
    "server": str,
}

NDP_DATASET_FIELDS: dict[str, type | tuple[type, ...]] = {
    "datasets": list,
    "count": int,
    "server": str,
}


_PARALLEL_TRIGGERS = (
    "validate ",
    "check ",
    "analyze ",
    "compare ",
    "profile ",
)

_MULTI_FILE_INTENT_TERMS = (
    "across",
    "all of",
    "all three",
    "both",
    "compare",
    "each",
    "fit together",
    "line up",
    "multi-file",
    "quality",
    "review",
    "same run",
    "sanity check",
    "these files",
    "triage",
    "together",
)

_NDP_INTENT_TERMS = (
    "catalog",
    "ckan",
    "dataset discovery",
    "discover datasets",
    "find datasets",
    "list organizations",
    "national data platform",
    "ndp",
    "search datasets",
)

_NDP_SEARCH_TERMS = (
    "carbon",
    "climate",
    "earth observation",
    "fire",
    "forest",
    "hurricane",
    "netcdf",
    "ocean",
    "precipitation",
    "temperature",
    "weather",
    "wildfire",
)


def _detect_parallel_items(question: str) -> list[str]:
    """Pull comma/and-separated items out of a "validate X, Y, and Z"
    style question. Empty list when the question doesn't match a
    parallel trigger or has only one item — no spawning in that case.

    Heuristic, not perfect — the goal is to surface obvious fan-out
    patterns. Tier-2 experts opt in by checking the result.
    """

    q = question.lower().strip()
    paths = extract_file_paths(
        question,
        "",
        {".h5", ".hdf5", ".parquet", ".csv", ".bp", ".bp4", ".bp5"},
    )
    if len(paths) >= 2 and (
        any(term in q for term in _MULTI_FILE_INTENT_TERMS)
        or len({path.suffix.lower() for path in paths}) >= 2
    ):
        return [str(path) for path in paths]
    if paths:
        return []

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

        # Filter to analysis-owned gateway tools.
        self._tools = [
            t for t in all_tools if t.name.startswith(("parquet_", "ndp_"))
        ]

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
            dspy.Prediction with analysis and recommendations fields.
            iowarp/clio-agent#9: when the question matches a parallel
            pattern ("validate X and Y" / "check X, Y, and Z"), spawn
            one Tier-3 nanoagent per item via
            ``clio_agent.runtime.nanoagent.spawn_many`` and attach
            the results to ``Prediction.nanoagents_spawned``. The
            GACT layer materialises them as child sessions.
        """
        nanoagents_spawned = self._spawn_tool_backed_nanoagents(question, file_context)
        items = _detect_parallel_items(question)
        if items and not nanoagents_spawned:
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

        if nanoagents_spawned and self._spawns_have_tool_provenance(nanoagents_spawned):
            result = self._aggregate_tool_backed_spawns(nanoagents_spawned)
        else:
            result = self.run(ExpertRequest(question=question, file_context=file_context))
        prediction = self._to_prediction(result)
        # iowarp/clio-agent#9: attach Tier-3 spawn provenance so the
        # GACT layer can render the child sessions.
        if nanoagents_spawned:
            try:
                prediction.nanoagents_spawned = nanoagents_spawned  # type: ignore[attr-defined]
            except Exception:
                pass
        return prediction

    @staticmethod
    def _spawns_have_tool_provenance(spawns: list[dict[str, Any]]) -> bool:
        """Return whether all worker rows include concrete tool-call provenance."""
        return bool(spawns) and all(spawn.get("tools_called") for spawn in spawns)

    @staticmethod
    def _aggregate_tool_backed_spawns(spawns: list[dict[str, Any]]) -> ExpertResult:
        """Build the parent answer and provenance from tool-backed nanoagent outputs."""
        answer_blocks: list[str] = []
        observations: list[ToolObservation] = []
        formats: list[str] = []

        for spawn in spawns:
            agent_id = str(spawn.get("agent_id") or "nanoagent")
            answer = str(spawn.get("answer") or "").strip()
            if answer:
                answer_blocks.append(f"{agent_id}:\n{answer}")
            if agent_id not in formats:
                formats.append(agent_id)

            for row in spawn.get("tools_called", []) or []:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or row.get("tool") or "").strip()
                if not name:
                    continue
                observations.append(
                    ToolObservation(
                        tool=name,
                        params=dict(row.get("args") or row.get("params") or {}),
                        result=row.get("result"),
                        duration_ms=float(row.get("duration_ms") or 0.0),
                        ok=bool(row.get("ok", True)),
                    )
                )

        analysis = (
            "Parallel validation completed with tool-backed nanoagents.\n\n"
            + "\n\n".join(answer_blocks)
        ).strip()
        recommendations = (
            "Use the independent worker findings together; each worker result is backed by "
            "the tool calls recorded in provenance."
        )
        return ExpertResult(
            analysis=analysis,
            recommendations=recommendations,
            source="deterministic",
            tools=tuple(observations),
            metadata={"expert": "analysis", "mode": "parallel_tool_backed", "workers": formats},
        )

    def _spawn_tool_backed_nanoagents(
        self,
        question: str,
        file_context: str,
    ) -> list[dict[str, Any]]:
        """Run deterministic tool-backed nanoagents for explicit multi-file validation."""
        if not _detect_parallel_items(question):
            return []

        spawns: list[dict[str, Any]] = []
        for hdf5_path in extract_file_paths(question, file_context, {".h5", ".hdf5"}):
            from clio_agent.experts.data_expert import DataExpert

            sub_question = f"Validate HDF5 structure for {hdf5_path}"
            result = DataExpert(tool_executor=self._tool_executor).run(
                ExpertRequest(question=sub_question, file_context=file_context)
            )
            spawns.append(self._tool_backed_spawn("data_validator", sub_question, result))

        for adios_path in extract_file_paths(question, file_context, {".bp", ".bp4", ".bp5"}):
            from clio_agent.experts.data_expert import DataExpert

            sub_question = f"Validate ADIOS/BP container for {adios_path}"
            result = DataExpert(tool_executor=self._tool_executor).run(
                ExpertRequest(question=sub_question, file_context=file_context)
            )
            spawns.append(self._tool_backed_spawn("adios_validator", sub_question, result))

        for parquet_path in extract_file_paths(question, file_context, {".parquet"}):
            sub_question = f"Validate Parquet statistics for {parquet_path}"
            result = self._inspect_parquet_file(
                ExpertRequest(question=sub_question, file_context=file_context),
                str(parquet_path),
            )
            spawns.append(self._tool_backed_spawn("analysis_validator", sub_question, result))

        for csv_path in extract_file_paths(question, file_context, {".csv"}):
            sub_question = f"Validate CSV schema for {csv_path}"
            result = self._inspect_csv_file(str(csv_path))
            spawns.append(self._tool_backed_spawn("csv_validator", sub_question, result))

        return spawns

    @staticmethod
    def _tool_backed_spawn(
        agent_id: str,
        question: str,
        result: ExpertResult,
    ) -> dict[str, Any]:
        """Convert an ExpertResult into the nanoagent wire shape."""
        return {
            "agent_id": agent_id,
            "input": {"question": question},
            "answer": f"{result.analysis}\n\n{result.recommendations}".strip(),
            "tools_called": [
                {
                    "name": observation.tool,
                    "args": observation.params,
                    "result": observation.result,
                    "duration_ms": observation.duration_ms,
                    "cached": False,
                    "ok": observation.ok,
                    "telemetry_source": "nanoagent",
                }
                for observation in result.tools
            ],
        }

    def run(self, request: ExpertRequest) -> ExpertResult:
        """Run the typed native expert boundary."""
        parquet_paths = extract_file_paths(request.question, request.file_context, {".parquet"})
        if parquet_paths:
            return self._inspect_parquet_file(request, str(parquet_paths[0]))

        csv_paths = extract_file_paths(request.question, request.file_context, {".csv"})
        if csv_paths:
            return self._inspect_csv_file(str(csv_paths[0]))

        if self._wants_ndp_discovery(request.question):
            return self._inspect_ndp_request(request)

        return self._synthesize_without_tools(request)

    def _inspect_parquet_file(self, request: ExpertRequest, filepath: str) -> ExpertResult:
        """Inspect a concrete Parquet path through deterministic gateway tools."""
        runner = NativeToolRunner(self._tool_executor)
        schema = runner.call("parquet_analyze_schema", {"filepath": filepath})
        schema_valid = validate_tool_result(
            "parquet_analyze_schema",
            schema,
            PARQUET_SCHEMA_FIELDS,
        )
        if not schema_valid.ok:
            assert schema_valid.error is not None
            runner.mark_validation_error("parquet_analyze_schema", schema_valid.error)
            return self._parquet_failure_result(filepath, schema_valid.error, runner)

        schema_data = schema_valid.data or {}
        columns_valid = validate_tool_items(
            "parquet_analyze_schema",
            schema_data,
            "columns",
            PARQUET_COLUMN_FIELDS,
        )
        if not columns_valid.ok:
            assert columns_valid.error is not None
            runner.mark_validation_error("parquet_analyze_schema", columns_valid.error)
            return self._parquet_failure_result(filepath, columns_valid.error, runner)

        columns = schema_data["columns"]
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
            stats_valid = validate_tool_result(
                "parquet_compute_statistics",
                stats,
                PARQUET_STATS_FIELDS,
            )
            if not stats_valid.ok:
                assert stats_valid.error is not None
                runner.mark_validation_error("parquet_compute_statistics", stats_valid.error)
                stats_lines.append(
                    f"{name}: statistics unavailable ({format_tool_error(stats_valid.error)})"
                )
                continue

            stats_data = stats_valid.data or {}
            stats_bits = [
                f"{k}={stats_data[k]}"
                for k in ("min", "max", "mean", "median", "std", "null_count", "unique_count")
                if k in stats_data
            ]
            stats_lines.append(f"{name}: " + ", ".join(stats_bits))

        analysis = (
            f"Inspected Parquet file {filepath}. It has {schema_data['num_rows']} rows, "
            f"{schema_data['num_columns']} columns, and {schema_data['num_row_groups']} "
            "row groups.\n"
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
    def _parquet_failure_result(
        filepath: str,
        error: dict[str, Any],
        runner: NativeToolRunner,
    ) -> ExpertResult:
        return ExpertResult(
            analysis=f"Could not inspect Parquet file {filepath}: {format_tool_error(error)}",
            recommendations="Verify the path, file readability, and Parquet tool contract.",
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
            schema_rows = [
                {
                    "name": field.name,
                    "type": str(field.type),
                    "null_count": table.column(field.name).null_count,
                }
                for field in table.schema
            ]
            tool_result: dict[str, Any] = {
                "rows": table.num_rows,
                "columns": table.num_columns,
                "filepath": str(safe_path),
                "schema": schema_rows,
            }
        except FilePolicyError as exc:
            result = normalize_tool_result(exc.to_result(), tool="csv_read_table")
            return ExpertResult(
                analysis=(
                    f"Could not inspect CSV file {filepath}: {format_tool_error(result['error'])}"
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
            result = {
                "error": normalize_tool_error(exc, tool="csv_read_table", code="tool_exception")
            }
            return ExpertResult(
                analysis=(
                    f"Could not inspect CSV file {filepath}: {format_tool_error(result['error'])}"
                ),
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

        duration_ms = (time.time() - start) * 1000
        result_valid = validate_tool_result("csv_read_table", tool_result, CSV_RESULT_FIELDS)
        if not result_valid.ok:
            assert result_valid.error is not None
            return self._csv_validation_failure(filepath, result_valid.error, duration_ms)

        csv_data = result_valid.data or {}
        columns_valid = validate_tool_items(
            "csv_read_table",
            csv_data,
            "schema",
            CSV_COLUMN_FIELDS,
        )
        if not columns_valid.ok:
            assert columns_valid.error is not None
            return self._csv_validation_failure(filepath, columns_valid.error, duration_ms)

        observation = ToolObservation(
            tool="csv_read_table",
            params={"filepath": filepath},
            result=tool_result,
            duration_ms=duration_ms,
            ok=tool_result_ok(tool_result),
        )

        column_lines = [
            f"- {field['name']}: {field['type']}, nulls={field['null_count']}"
            for field in csv_data["schema"]
        ]

        analysis = (
            f"Inspected CSV file {csv_data['filepath']}. It has {csv_data['rows']} rows and "
            f"{csv_data['columns']} columns.\n"
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

    @staticmethod
    def _csv_validation_failure(
        filepath: str,
        error: dict[str, Any],
        duration_ms: float,
    ) -> ExpertResult:
        return ExpertResult(
            analysis=f"Could not inspect CSV file {filepath}: {format_tool_error(error)}",
            recommendations="Verify the CSV reader contract before using file-specific facts.",
            source="deterministic",
            tools=(
                ToolObservation(
                    tool="csv_read_table",
                    params={"filepath": filepath},
                    result={"error": error},
                    duration_ms=duration_ms,
                    ok=False,
                ),
            ),
            metadata={"expert": "analysis", "format": "csv", "filepath": filepath},
        )

    def _inspect_ndp_request(self, request: ExpertRequest) -> ExpertResult:
        """Discover NDP catalog rows through clio-kit MCP-backed gateway tools."""
        runner = NativeToolRunner(self._tool_executor)
        q_lower = request.question.lower()
        org_filter = self._ndp_organization_filter(request.question)
        search_terms = self._ndp_search_terms(request.question)
        resource_format = self._ndp_resource_format(request.question)

        organizations: dict[str, Any] | None = None
        if org_filter or "organization" in q_lower or "noaa" in q_lower:
            organizations = runner.call(
                "ndp_list_organizations",
                {"name_filter": org_filter, "server": "global"},
            )
            organizations_valid = validate_tool_result(
                "ndp_list_organizations",
                organizations,
                NDP_ORGANIZATION_FIELDS,
            )
            if not organizations_valid.ok:
                assert organizations_valid.error is not None
                runner.mark_validation_error(
                    "ndp_list_organizations", organizations_valid.error
                )
                return self._ndp_failure_result(
                    "list organizations", organizations_valid.error, runner
                )
            organizations = organizations_valid.data or {}

        should_search = any(
            term in q_lower
            for term in ("dataset", "discover", "find", "search", "data product")
        )
        datasets: dict[str, Any] | None = None
        if should_search:
            params: dict[str, Any] = {"server": "global", "limit": 5}
            if search_terms:
                params["search_terms"] = search_terms
            if resource_format:
                params["resource_format"] = resource_format
            datasets = runner.call("ndp_search_datasets", params)
            datasets_valid = validate_tool_result(
                "ndp_search_datasets",
                datasets,
                NDP_DATASET_FIELDS,
            )
            if not datasets_valid.ok:
                assert datasets_valid.error is not None
                runner.mark_validation_error("ndp_search_datasets", datasets_valid.error)
                return self._ndp_failure_result("search datasets", datasets_valid.error, runner)
            datasets = datasets_valid.data or {}

        if organizations is None and datasets is None:
            organizations = runner.call(
                "ndp_list_organizations",
                {"name_filter": org_filter, "server": "global"},
            )
            organizations_valid = validate_tool_result(
                "ndp_list_organizations",
                organizations,
                NDP_ORGANIZATION_FIELDS,
            )
            if not organizations_valid.ok:
                assert organizations_valid.error is not None
                runner.mark_validation_error(
                    "ndp_list_organizations", organizations_valid.error
                )
                return self._ndp_failure_result(
                    "list organizations", organizations_valid.error, runner
                )
            organizations = organizations_valid.data or {}

        analysis_lines = ["Queried the National Data Platform catalog through clio-kit MCP."]
        if organizations is not None:
            org_rows = [str(row) for row in organizations.get("organizations", [])[:8]]
            analysis_lines.append(
                f"Organizations matched: {organizations.get('count', 0)}"
                + (("\n- " + "\n- ".join(org_rows)) if org_rows else "")
            )
        if datasets is not None:
            dataset_lines = [
                self._ndp_dataset_summary_line(row)
                for row in datasets.get("datasets", [])[:5]
                if isinstance(row, dict)
            ]
            analysis_lines.append(
                f"Datasets matched: {datasets.get('count', 0)}"
                + (("\n- " + "\n- ".join(dataset_lines)) if dataset_lines else "")
            )

        recommendations = (
            "Use ndp_get_dataset_details with a dataset id or name before downloading data. "
            "The rows above are live catalog results, so availability and formats should be "
            "verified again at execution time."
        )
        return ExpertResult(
            analysis="\n\n".join(analysis_lines),
            recommendations=recommendations,
            source="deterministic",
            tools=runner.observations,
            metadata={"expert": "analysis", "format": "ndp", "source": "clio-kit"},
        )

    @staticmethod
    def _ndp_failure_result(
        action: str,
        error: dict[str, Any],
        runner: NativeToolRunner,
    ) -> ExpertResult:
        return ExpertResult(
            analysis=f"Could not {action} in NDP: {format_tool_error(error)}",
            recommendations="Verify clio-kit is installed and the NDP endpoint is reachable.",
            source="deterministic",
            tools=runner.observations,
            metadata={"expert": "analysis", "format": "ndp", "source": "clio-kit"},
        )

    @staticmethod
    def _wants_ndp_discovery(question: str) -> bool:
        """Return whether the request asks for NDP/catalog discovery."""
        q_lower = question.lower()
        return any(term in q_lower for term in _NDP_INTENT_TERMS)

    @staticmethod
    def _ndp_organization_filter(question: str) -> str | None:
        """Extract an obvious organization filter from natural catalog requests."""
        q_lower = question.lower()
        if "noaa" in q_lower:
            return "noaa"
        if "nasa" in q_lower:
            return "nasa"
        if "doe" in q_lower:
            return "doe"
        return None

    @staticmethod
    def _ndp_search_terms(question: str) -> list[str]:
        """Extract conservative search terms for NDP dataset discovery."""
        q_lower = question.lower()
        return [term for term in _NDP_SEARCH_TERMS if term in q_lower]

    @staticmethod
    def _ndp_resource_format(question: str) -> str | None:
        """Extract common resource-format filters from a catalog request."""
        q_lower = question.lower()
        for fmt in ("csv", "json", "netcdf", "zarr", "hdf5", "parquet"):
            if fmt in q_lower:
                return fmt.upper()
        return None

    @staticmethod
    def _ndp_dataset_summary_line(row: dict[str, Any]) -> str:
        """Format one NDP dataset row for a compact expert answer."""
        title = str(row.get("title") or row.get("name") or row.get("id") or "<untitled>")
        owner = str(row.get("owner_org") or "unknown owner")
        resources = row.get("resources") or []
        formats = sorted(
            {
                str(resource.get("format")).upper()
                for resource in resources
                if isinstance(resource, dict) and resource.get("format")
            }
        )
        format_text = ", ".join(formats[:5]) if formats else "formats not listed"
        return f"{title} ({owner}; {format_text})"

    def _synthesize_without_tools(self, request: ExpertRequest) -> ExpertResult:
        """Use DSPy for conceptual guidance when no file can be inspected."""
        synthesis = self.agent(
            question=request.question,
            file_context=request.file_context,
        )
        analysis = str(getattr(synthesis, "analysis", "")).strip()
        recommendations = str(getattr(synthesis, "recommendations", "")).strip()
        if not analysis:
            raise ValueError("AnalysisExpert synthesis returned an empty analysis.")
        return ExpertResult(
            analysis=analysis,
            recommendations=recommendations,
            source="dspy",
            metadata={"expert": "analysis", "mode": "conceptual_synthesis"},
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
                "assessment of tabular datasets (Parquet/CSV) and external dataset "
                "discovery through NDP/clio-kit MCP. Computes column-level statistics, "
                "identifies distributions, and flags data quality issues."
            ),
            "keywords": [
                "parquet",
                "csv",
                "statistics",
                "analysis",
                "schema",
                "distribution",
                "data quality",
                "columnar",
                "profiling",
                "null count",
                "outliers",
                "ndp",
                "national data platform",
                "dataset discovery",
                "catalog",
            ],
            "priority": 2,
        }
