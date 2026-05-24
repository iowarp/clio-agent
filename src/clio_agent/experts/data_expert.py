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
import re
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

ADIOS_FILE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "filepath": str,
    "format": str,
    "is_directory": bool,
    "total_size_bytes": int,
    "member_count": int,
    "members": list,
    "has_profiling": bool,
    "variable_count": int,
    "variables": dict,
    "variable_source": str,
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
    "seismic",
    "seismological",
    "temperature",
    "weather",
    "wildfire",
)


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
            if tool.name.startswith(("hdf5_", "adios_", "ndp_"))
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
        hdf5_paths = extract_file_paths(request.question, request.file_context, {".h5", ".hdf5"})
        adios_paths = extract_file_paths(
            request.question,
            request.file_context,
            {".bp", ".bp4", ".bp5"},
        )
        if adios_paths:
            return self._inspect_adios_file(request, str(adios_paths[0]))
        if self._wants_ndp_discovery(request.question):
            return self._inspect_ndp_request(request)
        if not hdf5_paths:
            return self._synthesize_without_tools(request)
        if self._wants_dataset_analysis(request.question):
            return self._inspect_hdf5_dataset_request(request, str(hdf5_paths[0]))
        return self._inspect_hdf5_file(request, str(hdf5_paths[0]))

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

    def _inspect_adios_file(self, request: ExpertRequest, filepath: str) -> ExpertResult:
        """Inspect a concrete ADIOS/BP path through deterministic gateway tools."""
        runner = NativeToolRunner(self._tool_executor)
        overview = runner.call("adios_inspect_file", {"filepath": filepath})
        overview_valid = validate_tool_result(
            "adios_inspect_file",
            overview,
            ADIOS_FILE_FIELDS,
        )
        if not overview_valid.ok:
            assert overview_valid.error is not None
            runner.mark_validation_error("adios_inspect_file", overview_valid.error)
            return self._adios_failure_result(filepath, overview_valid.error, runner)

        data = overview_valid.data or {}
        members = data["members"]
        member_lines = [
            f"- {row['name']}: {format_bytes(int(row['size_bytes']))}"
            for row in members[:8]
            if isinstance(row, dict) and "name" in row and "size_bytes" in row
        ]
        if len(members) > 8:
            member_lines.append(f"- ... {len(members) - 8} more members")

        profiling = data.get("profiling")
        profiling_line = "No ADIOS profiling.json was found."
        if isinstance(profiling, dict):
            profiling_line = (
                f"Profiling covers {profiling.get('rank_count', 0)} ranks with "
                f"{format_bytes(int(profiling.get('transport_write_bytes') or 0))} "
                "of transport writes."
            )

        adios2_status = data.get("adios2_status")
        variable_line = (
            f"ADIOS2 variable metadata is available for {data['variable_count']} variables."
        )
        if isinstance(adios2_status, dict):
            variable_line = (
                "ADIOS2 variable metadata is not available in this environment: "
                f"{adios2_status.get('message', adios2_status)}"
            )

        analysis = (
            f"Inspected ADIOS/{data['format']} container {data['filepath']}. "
            f"It has {data['member_count']} members and totals "
            f"{format_bytes(data['total_size_bytes'])}.\n"
            + ("\n".join(member_lines) if member_lines else "No BP member files were found.")
            + "\n\n"
            + profiling_line
            + "\n"
            + variable_line
        )
        recommendations = (
            "Use profiling metadata for I/O health checks. Install ADIOS2 bindings when "
            "variable-level BP inspection is required, then re-run adios_inspect_variables."
        )
        if "variable" in request.question.lower() and isinstance(adios2_status, dict):
            recommendations = (
                "Variable-level inspection was requested but ADIOS2 is unavailable. "
                f"{adios2_status.get('next_action', 'Install ADIOS2 and retry.')}"
            )

        return ExpertResult(
            analysis=analysis,
            recommendations=recommendations,
            source="deterministic",
            tools=runner.observations,
            metadata={
                "expert": "data",
                "format": "adios",
                "filepath": filepath,
                "variable_source": data["variable_source"],
            },
        )

    @staticmethod
    def _adios_failure_result(
        filepath: str,
        error: dict[str, Any],
        runner: NativeToolRunner,
    ) -> ExpertResult:
        return ExpertResult(
            analysis=f"Could not inspect ADIOS/BP path {filepath}: {format_tool_error(error)}",
            recommendations="Verify the path, allowed roots, and ADIOS/BP tool contract.",
            source="deterministic",
            tools=runner.observations,
            metadata={"expert": "data", "format": "adios", "filepath": filepath},
        )

    def _inspect_ndp_request(self, request: ExpertRequest) -> ExpertResult:
        """Discover external datasets through clio-kit-backed NDP tools."""
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
            datasets_result = self._search_ndp_datasets(
                runner,
                search_terms=search_terms,
                resource_format=resource_format,
            )
            if isinstance(datasets_result.get("error"), dict):
                return self._ndp_failure_result(
                    "search datasets",
                    datasets_result["error"],
                    runner,
                )
            datasets = datasets_result

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
            dataset_rows = [
                row for row in datasets.get("datasets", [])[:5] if isinstance(row, dict)
            ]
            dataset_lines = [
                self._ndp_dataset_summary_line(row)
                for row in dataset_rows
            ]
            analysis_lines.append(
                f"Datasets matched: {datasets.get('count', 0)}"
                + (("\n- " + "\n- ".join(dataset_lines)) if dataset_lines else "")
            )
            contextual = self._ndp_contextual_analysis(request.question, dataset_rows)
            if contextual:
                analysis_lines.append(contextual)
            staging_note = self._ndp_staging_attempt(request.question, dataset_rows, runner)
            if staging_note:
                analysis_lines.append(staging_note)

        recommendations = self._ndp_recommendations(
            request.question,
            datasets.get("datasets", []) if datasets else [],
        )
        return ExpertResult(
            analysis="\n\n".join(analysis_lines),
            recommendations=recommendations,
            source="deterministic",
            tools=runner.observations,
            metadata={
                "expert": "data",
                "format": "ndp",
                "source": "clio-kit",
                "tier3_agent": "ndp_catalog",
            },
        )

    def _search_ndp_datasets(
        self,
        runner: NativeToolRunner,
        *,
        search_terms: list[str],
        resource_format: str | None,
    ) -> dict[str, Any]:
        """Search NDP with independent terms, then merge/dedupe dataset rows."""

        query_sets = [[term] for term in search_terms] or [[]]
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        first_error: dict[str, Any] | None = None
        valid_calls = 0

        for terms in query_sets:
            params: dict[str, Any] = {"server": "global", "limit": 5}
            if terms:
                params["search_terms"] = terms
            if resource_format:
                params["resource_format"] = resource_format

            result = runner.call("ndp_search_datasets", params)
            datasets_valid = validate_tool_result(
                "ndp_search_datasets",
                result,
                NDP_DATASET_FIELDS,
            )
            if not datasets_valid.ok:
                assert datasets_valid.error is not None
                runner.mark_validation_error("ndp_search_datasets", datasets_valid.error)
                if first_error is None:
                    first_error = datasets_valid.error
                continue

            valid_calls += 1
            data = datasets_valid.data or {}
            for row in data.get("datasets", []):
                if not isinstance(row, dict):
                    continue
                key = str(row.get("id") or row.get("name") or row.get("title") or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                rows.append(row)

        if valid_calls == 0 and first_error is not None:
            return {"error": first_error}

        return {"datasets": rows[:5], "count": len(rows), "server": "global"}

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
            metadata={
                "expert": "data",
                "format": "ndp",
                "source": "clio-kit",
                "tier3_agent": "ndp_catalog",
            },
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
        if "seismic" in q_lower or "seismological" in q_lower:
            return "seism"
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
        formats.extend(str(fmt).upper() for fmt in row.get("resource_formats", []) if fmt)
        formats = sorted(set(formats))
        resource_names = [str(name) for name in row.get("resource_names", []) if name]
        format_text = ", ".join(formats[:5]) if formats else "formats not listed"
        resource_text = f"; resources: {', '.join(resource_names[:2])}" if resource_names else ""
        return f"{title} ({owner}; {format_text}{resource_text})"

    @staticmethod
    def _ndp_contextual_analysis(question: str, rows: list[dict[str, Any]]) -> str:
        """Return domain-specific discovery notes grounded in catalog rows."""
        q_lower = question.lower()
        if not any(term in q_lower for term in ("seismic", "seismological", "three axes")):
            return ""

        candidate = DataExpert._select_seismic_dataset(rows)
        if candidate is None:
            return (
                "Seismic workflow note: no catalog row clearly exposes waveform data. "
                "Do not route to analysis or visualization until a downloadable waveform "
                "resource has been staged."
            )

        title = str(candidate.get("title") or candidate.get("name") or candidate.get("id"))
        notes = str(candidate.get("notes") or "")
        resource_names = ", ".join(str(name) for name in candidate.get("resource_names", [])[:3])
        format_hint = "MiniSEED" if "miniseed" in (notes + resource_names).lower() else "waveform"
        return (
            "Seismic workflow note: the best discovery-stage candidate is "
            f"{title!r}. Its catalog text/resource names indicate {format_hint} waveform "
            "data"
            + (f" ({resource_names})." if resource_names else ".")
            + " CLIO has not downloaded or opened that resource yet, so analysis and "
            "three-axis plotting remain blocked on staging the waveform file."
        )

    @staticmethod
    def _ndp_staging_attempt(
        question: str,
        rows: list[dict[str, Any]],
        runner: NativeToolRunner,
    ) -> str:
        """Attempt data-stage resource staging when the prompt asks beyond discovery."""
        q_lower = question.lower()
        if not any(
            term in q_lower
            for term in (
                "analyze",
                "download",
                "inspect the data",
                "open the data",
                "plot",
                "stage",
                "three-axis",
                "three axes",
            )
        ):
            return ""

        candidate = DataExpert._select_seismic_dataset(rows) or (rows[0] if rows else None)
        if candidate is None:
            return (
                "Staging note: no dataset candidate was available, so CLIO did not "
                "attempt resource staging."
            )

        identifier = str(candidate.get("id") or candidate.get("name") or "").strip()
        if not identifier:
            return (
                "Staging note: the selected dataset did not expose an id or name, so "
                "CLIO could not request detailed resource metadata."
            )

        identifier_type = "id" if candidate.get("id") else "name"
        details = runner.call(
            "ndp_get_dataset_details",
            {
                "dataset_identifier": identifier,
                "identifier_type": identifier_type,
                "server": "global",
            },
        )
        if isinstance(details, dict) and details.get("error"):
            return (
                "Staging note: dataset detail lookup failed before download: "
                f"{format_tool_error(details['error'])}"
            )

        staged = runner.call(
            "ndp_stage_resource",
            {
                "dataset_identifier": identifier,
                "identifier_type": identifier_type,
                "resource_index": 0,
                "server": "global",
            },
        )
        if isinstance(staged, dict) and staged.get("staged"):
            return (
                "Staging note: CLIO staged the selected NDP resource at "
                f"{staged.get('path')}. Analysis and visualization can now use that "
                "local file if the format is supported."
            )
        if isinstance(staged, dict) and staged.get("error"):
            code = staged["error"].get("code") if isinstance(staged["error"], dict) else None
            code_text = f" [{code}]" if code else ""
            return (
                "Staging note: CLIO attempted to stage the selected NDP resource, but "
                f"staging failed visibly{code_text}: {format_tool_error(staged['error'])}"
            )
        return (
            "Staging note: CLIO attempted resource staging but received an unexpected "
            "result shape, so downstream analysis remains blocked."
        )

    @staticmethod
    def _ndp_recommendations(question: str, rows: list[Any]) -> str:
        """Return next actions for the data-stage NDP discovery result."""
        q_lower = question.lower()
        if any(term in q_lower for term in ("seismic", "seismological", "three axes")):
            return (
                "Treat this as a data discovery result, not completed analysis. Next: use "
                "ndp_get_dataset_details or the NDP resource page to obtain the resource URL, "
                "download/stage the MiniSEED waveform with ndp_stage_resource or a "
                "Pelican client, inspect channels/stations with a "
                "seismic reader such as ObsPy, then pass the staged three-component traces "
                "to analysis and visualization."
            )
        return (
            "Treat these as discovery results owned by the data stage. Use "
            "ndp_get_dataset_details with a dataset id or name before downloading, then "
            "stage a concrete resource before routing quantitative work to analysis."
        )

    @staticmethod
    def _select_seismic_dataset(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Choose the most analysis-ready seismic row from compact NDP results."""
        for row in rows:
            haystack = " ".join(
                str(value)
                for value in (
                    row.get("title"),
                    row.get("name"),
                    row.get("notes"),
                    " ".join(str(name) for name in row.get("resource_names", [])),
                )
                if value
            ).lower()
            if "miniseed" in haystack or ("seismic" in haystack and "waveform" in haystack):
                return row
        return None

    @staticmethod
    def _wants_dataset_analysis(question: str) -> bool:
        q_lower = question.lower()
        if any(
            token in q_lower
            for token in ("hdf5_analyze_dataset", "analyze_dataset", "analyze dataset")
        ):
            return True
        if re.search(r"\b[\w.-]+/[\w./-]+\b", question):
            return any(
                token in q_lower
                for token in (
                    "chunk",
                    "compression",
                    "dataset",
                    "deep dive",
                    "focus",
                    "inside",
                    "read pattern",
                    "stats",
                    "statistics",
                    "zoom",
                )
            )
        return False

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
                "Specializes in scientific data files and discovery: HDF5, ADIOS/BP, "
                "compression strategies, I/O performance, format conversion, and "
                "external dataset discovery through NDP/clio-kit MCP"
            ),
            "keywords": [
                "hdf5",
                "adios",
                "bp5",
                "ndp",
                "national data platform",
                "dataset discovery",
                "catalog",
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
