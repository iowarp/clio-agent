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
from pathlib import Path
from typing import Any, Optional

import dspy

from clio_agent.experts.native_tools import NativeToolRunner
from clio_agent.experts.sac_format_expert import SAC_SUFFIXES, SACFormatExpert
from clio_agent.harness import (
    FILE_PATH_RE,
    QUOTED_FILE_PATH_RE,
    WINDOWS_FILE_PATH_RE,
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

_PARALLEL_TRIGGERS = (
    "validate ",
    "check ",
    "analyze ",
    "compare ",
    "profile ",
)

_PARALLEL_FILE_SUFFIXES = {
    ".h5",
    ".hdf5",
    ".parquet",
    ".csv",
    ".bp",
    ".bp4",
    ".bp5",
    ".sac",
    ".tar",
    ".tgz",
    ".gz",
}

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
    "validate",
)


def _extract_raw_question_file_paths(question: str, suffixes: set[str]) -> list[str]:
    """Return explicit file paths as written in the question.

    ``extract_file_paths`` intentionally resolves relative paths for execution.
    Parallel fan-out labels should instead preserve the user's prompt text so
    generated worker names and tests do not depend on the host OS path rules.
    """

    candidates: list[tuple[int, int, int, str]] = []
    for match in QUOTED_FILE_PATH_RE.finditer(question):
        candidates.append((match.start(), match.end(), 0, match.group("path")))
    for match in WINDOWS_FILE_PATH_RE.finditer(question):
        candidates.append((match.start(), match.end(), 1, match.group("path")))
    for match in FILE_PATH_RE.finditer(question):
        candidates.append((match.start(), match.end(), 2, match.group("path")))

    raw_paths: list[str] = []
    seen: set[str] = set()
    handled_spans: list[tuple[int, int]] = []
    for start, end, _priority, raw_path in sorted(candidates):
        if any(span_start <= start < span_end for span_start, span_end in handled_spans):
            continue
        handled_spans.append((start, end))
        cleaned = raw_path.rstrip(".,;:)]}")
        if Path(cleaned).suffix.lower() not in suffixes:
            continue
        if cleaned not in seen:
            raw_paths.append(cleaned)
            seen.add(cleaned)
    return raw_paths


def _detect_parallel_items(question: str) -> list[str]:
    """Pull comma/and-separated items out of a "validate X, Y, and Z"
    style question. Empty list when the question doesn't match a
    parallel trigger or has only one item — no spawning in that case.

    Heuristic, not perfect — the goal is to surface obvious fan-out
    patterns. Tier-2 experts opt in by checking the result.
    """

    q = question.lower().strip()
    raw_paths = _extract_raw_question_file_paths(question, _PARALLEL_FILE_SUFFIXES)
    paths = extract_file_paths(question, "", _PARALLEL_FILE_SUFFIXES)
    if len(paths) >= 2 and (
        any(term in q for term in _MULTI_FILE_INTENT_TERMS)
        or len({path.suffix.lower() for path in paths}) >= 2
    ):
        return raw_paths if len(raw_paths) >= 2 else [str(path) for path in paths]
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
        self.sac_format_expert = SACFormatExpert(tool_executor=self._tool_executor)
        all_tools = self._tool_executor.to_dspy_tools()

        # Filter to analysis-owned gateway tools.
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

        for seismic_path in extract_file_paths(question, file_context, SAC_SUFFIXES):
            sub_question = f"Validate SAC waveform statistics for {seismic_path}"
            result = self.sac_format_expert.compute_trace_statistics(str(seismic_path))
            spawns.append(self._tool_backed_spawn("sac_format_validator", sub_question, result))

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
        question_parquet_paths = extract_file_paths(request.question, "", {".parquet"})
        question_csv_paths = extract_file_paths(request.question, "", {".csv"})
        question_seismic_paths = extract_file_paths(request.question, "", SAC_SUFFIXES)
        explicit_families = sum(
            bool(paths)
            for paths in (question_parquet_paths, question_csv_paths, question_seismic_paths)
        )
        if explicit_families == 0 and self._should_synthesize_multi_source_evidence(request):
            return self._synthesize_without_tools(request, include_retained_evidence_anchors=True)

        if question_parquet_paths:
            return self._inspect_parquet_file(request, str(question_parquet_paths[0]))
        if question_csv_paths:
            return self._inspect_csv_file(str(question_csv_paths[0]))
        if question_seismic_paths:
            return self.sac_format_expert.compute_trace_statistics(str(question_seismic_paths[0]))

        parquet_paths = extract_file_paths("", request.file_context, {".parquet"})
        if parquet_paths:
            return self._inspect_parquet_file(request, str(parquet_paths[0]))

        csv_paths = extract_file_paths("", request.file_context, {".csv"})
        if csv_paths:
            return self._inspect_csv_file(str(csv_paths[0]))

        seismic_paths = extract_file_paths("", request.file_context, SAC_SUFFIXES)
        if seismic_paths:
            return self.sac_format_expert.compute_trace_statistics(str(seismic_paths[0]))

        return self._synthesize_without_tools(request)

    @classmethod
    def _should_synthesize_multi_source_evidence(cls, request: ExpertRequest) -> bool:
        """Return whether the request is a broad synthesis over retained evidence.

        The analysis expert should not narrow a readiness/review/compare question
        to the first concrete path found in retained context. Format-specific
        native tools are still used for direct inspection questions and explicit
        validation fan-out handled before ``run()``.
        """
        combined = "\n".join([request.question, request.file_context]).lower()
        if "[retained session context]" not in combined and "[compact summary]" not in combined:
            return False

        if not cls._asks_for_multi_source_synthesis(request.question):
            return False

        families = set()
        suffix_groups: tuple[tuple[str, set[str]], ...] = (
            ("hdf5", {".h5", ".hdf5"}),
            ("parquet", {".parquet"}),
            ("csv", {".csv"}),
            ("adios", {".bp", ".bp4", ".bp5"}),
            ("sac", SAC_SUFFIXES),
        )
        for family, suffixes in suffix_groups:
            if extract_file_paths(request.question, request.file_context, suffixes):
                families.add(family)
        return len(families) >= 2

    @staticmethod
    def _asks_for_multi_source_synthesis(question: str) -> bool:
        """Return whether a question asks for synthesis, review, or comparison."""
        lowered = question.lower()
        synthesis_terms = (
            "all stages",
            "all files",
            "multi-source",
            "multiple source",
            "cross-file",
            "hdf5",
            "bp5",
            "adios",
            "csv",
            "compare",
            "line up",
            "across",
            "collaborator",
            "review",
            "strongest",
            "cite the strongest evidence",
            "what still needs checking",
        )
        return any(term in lowered for term in synthesis_terms)

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
        wants_stats = any(
            token in q_lower
            for token in ("stat", "profile", "quality", "null", "anomaly", "triage", "outlier")
        )
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
            selected: list[str] = []
            if "anomaly" in q_lower:
                selected.extend(
                    str(col["name"])
                    for col in columns
                    if col.get("name") and "anomaly" in str(col["name"]).lower()
                )
            for col in columns:
                name = str(col.get("name", ""))
                dtype = str(col.get("type", "")).lower()
                if not name or name in selected:
                    continue
                if name.lower().endswith("_id") or name.lower() in {"id", "run_id"}:
                    continue
                if any(token in dtype for token in ("int", "float", "double", "decimal")):
                    selected.append(name)
                if len(selected) >= 4:
                    break
            if selected:
                return selected[:4]
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

    def _synthesize_without_tools(
        self,
        request: ExpertRequest,
        *,
        include_retained_evidence_anchors: bool = False,
    ) -> ExpertResult:
        """Use DSPy for conceptual guidance when no file can be inspected."""
        synthesis = self.agent(
            question=request.question,
            file_context=request.file_context,
        )
        analysis = str(getattr(synthesis, "analysis", "")).strip()
        recommendations = str(getattr(synthesis, "recommendations", "")).strip()
        if not analysis or analysis.lower() in {"none", "null", "n/a"}:
            raise ValueError("AnalysisExpert synthesis returned an empty analysis.")
        if include_retained_evidence_anchors:
            anchors = self._retained_evidence_anchor_text(request.file_context)
            if anchors:
                analysis = f"{analysis.rstrip()}\n\n{anchors}"
        return ExpertResult(
            analysis=analysis,
            recommendations=recommendations,
            source="dspy",
            metadata={"expert": "analysis", "mode": "conceptual_synthesis"},
        )

    @staticmethod
    def _retained_evidence_anchor_text(file_context: str, *, max_lines: int = 36) -> str:
        """Return exact retained identifiers from compacted context.

        This is intentionally a labeled evidence appendix, not a synthesized
        answer. It prevents later provider synthesis from dropping exact paths,
        variable names, and caveat markers that survived compaction.
        """
        marker = "[exact retained evidence index]"
        marker_at = file_context.lower().find(marker)
        if marker_at < 0:
            return ""

        section = file_context[marker_at:].splitlines()
        kept: list[str] = []
        current_heading = ""
        allowed_headings = {"paths:", "identifiers:", "caveats/errors:"}
        for raw_line in section[1:]:
            line = raw_line.strip()
            if not line:
                continue
            if line.lower() in allowed_headings:
                current_heading = line
                kept.append(line)
                continue
            if not current_heading or not line.startswith("- "):
                continue
            kept.append(line)
            if len(kept) >= max_lines:
                break

        if not kept:
            return ""
        return (
            "Retained evidence anchors (exact identifiers from compacted context):\n"
            + "\n".join(kept)
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
                "assessment of tabular datasets (Parquet/CSV). Delegates waveform "
                "format-specific work to nested format experts."
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
            ],
            "priority": 2,
        }
