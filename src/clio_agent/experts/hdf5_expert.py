"""
HDF5 Expert.

A specialized DSPy expert for HDF5 files: inspection, layout/filter
changes, visualization, CF-conventions checks, and on-demand depth from
the bundled SKILL.md library. Pairs with the DataExpert (broader
scientific-data scope, Parquet) but wins on HDF5-specific routing thanks
to a richer keyword set.

Architecture
------------
- Deterministic-first: when the question names a concrete ``.h5`` /
  ``.hdf5`` / ``.nc`` file, the expert routes the question to one of
  five action tools by inspecting verbs in the question text. No DSPy
  call is made on this path.
- Conceptual fallback: when no file is present, the expert pulls the
  best-matching skill body via ``match_skills`` and injects it into
  ``file_context`` before invoking ``dspy.Predict(HDF5ExpertSignature)``.
- Tool surface: an explicit 7-tool allowlist (NOT a prefix scan) so the
  expert's LLM-visible surface stays curated even as the underlying MCP
  server grows.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import dspy

from clio_agent.experts.hdf5_skills import (
    SkillNotFoundError,
    load_skill,
    match_skills,
)
from clio_agent.experts.native_tools import NativeToolRunner
from clio_agent.harness import (
    ExpertRequest,
    ExpertResult,
    extract_file_paths,
    format_tool_error,
    validate_tool_result,
)
from clio_agent.signatures.hdf5_sig import HDF5ExpertSignature
from clio_agent.tools.execution import ToolExecutor, create_sync_tool_executor
from clio_agent.tools.gateway import gateway

logger = logging.getLogger(__name__)


# Explicit per-expert tool allowlist. Adding/removing here is the *only*
# place that affects what the HDF5Expert exposes to a ReAct-style LLM
# loop; new tools added to hdf5_server.py do not leak in automatically.
_HDF5_EXPERT_TOOLS: tuple[str, ...] = (
    "hdf5_analyze_file",
    "hdf5_get_object_metadata",
    "hdf5_rechunk_dataset",
    "hdf5_apply_filter",
    "hdf5_visualize_dataset",
    "hdf5_check_cf_compliance",
    "hdf5_consult_skill",
)

_HDF5_EXTENSIONS = frozenset({".h5", ".hdf5", ".nc"})

# Schema constants for every tool reply the expert validates. Mirrors the
# pattern in DataExpert: catch tool drift at the call site so a renamed
# field never silently propagates into a hallucinated analysis.

_HDF5_ANALYZE_FILE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "filepath": str,
    "file_size_bytes": int,
    "total_datasets": int,
    "total_groups": int,
    "datasets": list,
    "groups": list,
    "compression_summary": dict,
}

_HDF5_OBJECT_METADATA_FIELDS: dict[str, type | tuple[type, ...]] = {
    "filepath": str,
    "name": str,
    "object_type": str,
}

_HDF5_RECHUNK_FIELDS: dict[str, type | tuple[type, ...]] = {
    "success": bool,
    "output_filepath": str,
    "object_path": str,
    "dataset_shape": list,
}

_HDF5_FILTER_FIELDS: dict[str, type | tuple[type, ...]] = {
    "success": bool,
    "output_filepath": str,
    "object_path": str,
    "original_filters": dict,
    "new_filters": dict,
}

_HDF5_VISUALIZE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "success": bool,
    "save_path": str,
    "object_path": str,
    "plot_type": str,
    "shape": list,
}

_HDF5_CF_FIELDS: dict[str, type | tuple[type, ...]] = {
    "status": str,
    "filepath": str,
    "file_format": str,
    "score_percent": (int, float),
    "issue_counts": dict,
    "issues": list,
}

_HDF5_SKILL_FIELDS: dict[str, type | tuple[type, ...]] = {
    "skill_name": str,
    "description": str,
    "body": str,
    "alternatives": list,
}


class HDF5Expert(dspy.Module):
    """HDF5-specialized expert with deterministic dispatch and skill-backed
    synthesis fallback.

    Attributes:
        arc_memory: Optional ARC memory instance for caching / provenance.

    Example:
        >>> expert = HDF5Expert()
        >>> result = expert(
        ...     question="Get metadata for /simulation/temperature in /tmp/x.h5",
        ...     file_context="",
        ... )
        >>> print(result.analysis)
    """

    def __init__(
        self,
        arc_memory: Optional[Any] = None,
        tool_executor: Optional[ToolExecutor] = None,
    ) -> None:
        super().__init__()
        self.arc_memory = arc_memory
        self._tool_executor = tool_executor or create_sync_tool_executor(gateway)
        # Curated allowlist — see DECISIONS §5 for why this is explicit
        # rather than a startswith("hdf5_") filter.
        allowlist = set(_HDF5_EXPERT_TOOLS)
        self._tools = [
            tool
            for tool in self._tool_executor.to_dspy_tools()
            if tool.name in allowlist
        ]
        logger.info(
            "HDF5Expert initialized with %d/%d allowlisted tools: %s",
            len(self._tools),
            len(allowlist),
            [t.name for t in self._tools],
        )
        self.agent = dspy.Predict(HDF5ExpertSignature)

    def forward(self, question: str, file_context: str = "") -> dspy.Prediction:
        """DSPy entry point. Wraps the typed ``run`` boundary."""
        result = self.run(ExpertRequest(question=question, file_context=file_context))
        return self._to_prediction(result)

    def run(self, request: ExpertRequest) -> ExpertResult:
        """Typed expert boundary. Dispatches to a deterministic tool path
        when a file is present in the question or context; otherwise
        delegates to skill-augmented DSPy synthesis."""
        paths = extract_file_paths(request.question, request.file_context, _HDF5_EXTENSIONS)
        if not paths:
            return self._synthesize_without_file(request)

        filepath = str(paths[0])
        verb = self._classify_verb(request.question)
        if verb == "metadata":
            return self._dispatch_get_object_metadata(request, filepath)
        if verb == "rechunk":
            return self._dispatch_rechunk(request, filepath)
        if verb == "filter":
            return self._dispatch_apply_filter(request, filepath)
        if verb == "visualize":
            return self._dispatch_visualize(request, filepath)
        if verb == "cf_compliance":
            return self._dispatch_cf_compliance(request, filepath)
        return self._dispatch_analyze_file(request, filepath)

    # ------------------------------------------------------------------
    # Question -> verb classification
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_verb(question: str) -> str:
        """Pick a deterministic tool by inspecting verbs in the question.

        Crude keyword routing — same pattern DataExpert uses. The order
        below matters: more specific verbs are checked first so
        "rechunk to align with column access" doesn't get classified as
        a plain metadata fetch.
        """
        q = question.lower()
        if any(
            tok in q
            for tok in (
                "rechunk",
                "re-chunk",
                "chunk layout",
                "change chunk",
                "make contiguous",
                "convert to contiguous",
            )
        ):
            return "rechunk"
        if any(
            tok in q
            for tok in (
                "apply filter",
                "compress",
                "decompress",
                "add gzip",
                "remove compression",
                "shuffle filter",
                "fletcher32",
                "change filter",
            )
        ):
            return "filter"
        if any(
            tok in q
            for tok in (
                "visualize",
                "plot ",
                "draw ",
                "render ",
                "histogram",
                "heatmap",
                "show me the",
                "graph the",
                "make a plot",
            )
        ):
            return "visualize"
        if "cf compliance" in q or "cf-compliance" in q or "cf conventions" in q:
            return "cf_compliance"
        if any(
            tok in q
            for tok in (
                "get_object_metadata",
                "metadata for",
                "metadata of",
                "inspect",
                "shape of",
                "dtype of",
                "attributes of",
            )
        ):
            return "metadata"
        return "analyze_file"

    # ------------------------------------------------------------------
    # Deterministic dispatch paths
    # ------------------------------------------------------------------

    def _dispatch_analyze_file(self, request: ExpertRequest, filepath: str) -> ExpertResult:
        runner = NativeToolRunner(self._tool_executor)
        result = runner.call("hdf5_analyze_file", {"filepath": filepath})
        validated = validate_tool_result("hdf5_analyze_file", result, _HDF5_ANALYZE_FILE_FIELDS)
        if not validated.ok:
            assert validated.error is not None
            runner.mark_validation_error("hdf5_analyze_file", validated.error)
            return self._failure(filepath, validated.error, runner, mode="analyze_file")
        data = validated.data or {}
        comp = data.get("compression_summary", {})
        analysis = (
            f"Inspected HDF5 file {filepath}: {data.get('total_datasets', 0)} datasets "
            f"in {data.get('total_groups', 0)} groups, "
            f"{int(comp.get('compressed_datasets', 0))} compressed."
        )
        recommendations = (
            "Call get_object_metadata on any dataset whose layout you want to inspect, "
            "or consult_skill('hdf5-chunking' / 'hdf5-filters') for sizing rules of thumb."
        )
        return ExpertResult(
            analysis=analysis,
            recommendations=recommendations,
            source="deterministic",
            tools=runner.observations,
            metadata={"expert": "hdf5", "mode": "analyze_file", "filepath": filepath},
        )

    def _dispatch_get_object_metadata(
        self, request: ExpertRequest, filepath: str
    ) -> ExpertResult:
        runner = NativeToolRunner(self._tool_executor)
        obj_path = self._extract_object_path(request.question) or "/"
        result = runner.call(
            "hdf5_get_object_metadata",
            {"filepath": filepath, "object_path": obj_path},
        )
        validated = validate_tool_result(
            "hdf5_get_object_metadata", result, _HDF5_OBJECT_METADATA_FIELDS
        )
        if not validated.ok:
            assert validated.error is not None
            runner.mark_validation_error("hdf5_get_object_metadata", validated.error)
            return self._failure(filepath, validated.error, runner, mode="metadata")
        data = validated.data or {}
        lines: list[str] = [
            f"Inspected '{data['name']}' in {filepath} (type: {data['object_type']})."
        ]
        if data["object_type"] == "dataset":
            lines.append(
                f"shape={data.get('shape')}, dtype={data.get('dtype')}, "
                f"chunks={data.get('chunks')}, compression={data.get('compression')}, "
                f"size_bytes={data.get('size_bytes')}"
            )
        elif data["object_type"] in ("group", "file_root"):
            members = data.get("members", [])
            preview = ", ".join(members[:8])
            more = "" if len(members) <= 8 else f" + {len(members) - 8} more"
            lines.append(f"members ({len(members)}): {preview}{more}")
        return ExpertResult(
            analysis="\n".join(lines),
            recommendations=(
                "Use the layout above to decide whether rechunk_dataset or "
                "apply_filter is warranted. Consult hdf5-chunking or hdf5-filters "
                "for the rule of thumb."
            ),
            source="deterministic",
            tools=runner.observations,
            metadata={
                "expert": "hdf5",
                "mode": "metadata",
                "filepath": filepath,
                "object_path": data["name"],
            },
        )

    def _dispatch_rechunk(self, request: ExpertRequest, filepath: str) -> ExpertResult:
        """Advisory mode: surface the planned chunk change instead of executing it.

        The HDF5Expert intentionally does NOT invoke h5repack automatically.
        It reports current chunking + a planned change so the user can
        confirm before mutation. The actual mutation goes through a
        deliberate follow-up call by the orchestrator with explicit
        parameters.
        """
        runner = NativeToolRunner(self._tool_executor)
        obj_path = self._extract_object_path(request.question) or "/"
        meta = runner.call(
            "hdf5_get_object_metadata",
            {"filepath": filepath, "object_path": obj_path},
        )
        validated = validate_tool_result(
            "hdf5_get_object_metadata", meta, _HDF5_OBJECT_METADATA_FIELDS
        )
        if not validated.ok:
            assert validated.error is not None
            runner.mark_validation_error("hdf5_get_object_metadata", validated.error)
            return self._failure(filepath, validated.error, runner, mode="rechunk_plan")
        data = validated.data or {}
        current_chunks = data.get("chunks")
        analysis = (
            f"Planned rechunk of '{data['name']}' in {filepath}.\n"
            f"Current chunks: {current_chunks}; shape={data.get('shape')}, "
            f"dtype={data.get('dtype')}.\n"
            "Advisory only — call hdf5_rechunk_dataset with explicit chunk_dims, "
            "chunk_adjustment, or make_contiguous=True to execute the change."
        )
        return ExpertResult(
            analysis=analysis,
            recommendations=(
                "Consult hdf5-chunking for sizing rules; pick chunk_dims that align "
                "with the dominant access pattern, then call hdf5_rechunk_dataset "
                "with explicit args."
            ),
            source="deterministic",
            tools=runner.observations,
            metadata={
                "expert": "hdf5",
                "mode": "rechunk_plan",
                "filepath": filepath,
                "object_path": data["name"],
            },
        )

    def _dispatch_apply_filter(self, request: ExpertRequest, filepath: str) -> ExpertResult:
        """Same advisory pattern as rechunk: report current filters, don't execute."""
        runner = NativeToolRunner(self._tool_executor)
        obj_path = self._extract_object_path(request.question) or "/"
        meta = runner.call(
            "hdf5_get_object_metadata",
            {"filepath": filepath, "object_path": obj_path},
        )
        validated = validate_tool_result(
            "hdf5_get_object_metadata", meta, _HDF5_OBJECT_METADATA_FIELDS
        )
        if not validated.ok:
            assert validated.error is not None
            runner.mark_validation_error("hdf5_get_object_metadata", validated.error)
            return self._failure(filepath, validated.error, runner, mode="filter_plan")
        data = validated.data or {}
        analysis = (
            f"Planned filter change on '{data['name']}' in {filepath}.\n"
            f"Current compression: {data.get('compression')!r} "
            f"(opts={data.get('compression_opts')}), shuffle={data.get('shuffle')}, "
            f"fletcher32={data.get('fletcher32')}.\n"
            "Advisory only — call hdf5_apply_filter with explicit filter_type "
            "(and compression_level / szip_options / scaleoffset_params) to execute."
        )
        return ExpertResult(
            analysis=analysis,
            recommendations=(
                "Consult hdf5-filters for filter selection trade-offs, then call "
                "hdf5_apply_filter with explicit args. shuffle + gzip is a common "
                "default for compressible float data."
            ),
            source="deterministic",
            tools=runner.observations,
            metadata={
                "expert": "hdf5",
                "mode": "filter_plan",
                "filepath": filepath,
                "object_path": data["name"],
            },
        )

    def _dispatch_visualize(self, request: ExpertRequest, filepath: str) -> ExpertResult:
        runner = NativeToolRunner(self._tool_executor)
        obj_path = self._extract_object_path(request.question)
        if not obj_path:
            return ExpertResult(
                analysis=(
                    f"Cannot visualize {filepath} without a dataset path. "
                    "Provide a path inside the file (e.g. '/group/temperature')."
                ),
                recommendations=(
                    "Call hdf5_analyze_file first to list datasets, then repeat "
                    "the visualize request with an explicit object_path."
                ),
                source="deterministic",
                tools=runner.observations,
                metadata={"expert": "hdf5", "mode": "visualize_missing_path"},
            )
        result = runner.call(
            "hdf5_visualize_dataset",
            {"filepath": filepath, "object_path": obj_path},
        )
        validated = validate_tool_result(
            "hdf5_visualize_dataset", result, _HDF5_VISUALIZE_FIELDS
        )
        if not validated.ok:
            assert validated.error is not None
            runner.mark_validation_error("hdf5_visualize_dataset", validated.error)
            return self._failure(filepath, validated.error, runner, mode="visualize")
        data = validated.data or {}
        return ExpertResult(
            analysis=(
                f"Plotted '{obj_path}' from {filepath} as a {data['plot_type']} "
                f"({data['shape']} -> sampled {data.get('sampled_shape')}). "
                f"PNG written to {data['save_path']}."
            ),
            recommendations=(
                "Open the PNG to inspect. For different plot styling, call "
                "hdf5_visualize_dataset with plot_type=line/hist/imshow/pcolormesh "
                "and a custom save_path. Consult hdf5-visualization for choices."
            ),
            source="deterministic",
            tools=runner.observations,
            metadata={
                "expert": "hdf5",
                "mode": "visualize",
                "filepath": filepath,
                "object_path": obj_path,
                "save_path": data["save_path"],
            },
        )

    def _dispatch_cf_compliance(self, request: ExpertRequest, filepath: str) -> ExpertResult:
        runner = NativeToolRunner(self._tool_executor)
        result = runner.call("hdf5_check_cf_compliance", {"filepath": filepath})
        validated = validate_tool_result(
            "hdf5_check_cf_compliance", result, _HDF5_CF_FIELDS
        )
        if not validated.ok:
            assert validated.error is not None
            runner.mark_validation_error("hdf5_check_cf_compliance", validated.error)
            return self._failure(filepath, validated.error, runner, mode="cf_compliance")
        data = validated.data or {}
        analysis = (
            f"CF compliance check on {filepath}: format={data['file_format']}, "
            f"declared_conventions={data.get('declared_conventions')!r}, "
            f"score={data['score_percent']}% over {data.get('total_datasets_checked', 0)} "
            f"datasets, issues high/medium/low="
            f"{data['issue_counts'].get('high', 0)}/"
            f"{data['issue_counts'].get('medium', 0)}/"
            f"{data['issue_counts'].get('low', 0)}."
        )
        return ExpertResult(
            analysis=analysis,
            recommendations=(
                "Consult hdf5-cf-compliance for what each missing attr means. For "
                "an authoritative report, run the IOOS compliance-checker offline."
            ),
            source="deterministic",
            tools=runner.observations,
            metadata={
                "expert": "hdf5",
                "mode": "cf_compliance",
                "filepath": filepath,
            },
        )

    # ------------------------------------------------------------------
    # Conceptual-fallback path (no file mentioned)
    # ------------------------------------------------------------------

    def _synthesize_without_file(self, request: ExpertRequest) -> ExpertResult:
        """No file to inspect — fetch the best-matching skill, inject its
        body into file_context, then call DSPy on the signature."""
        matches = match_skills(request.question, top_k=3)
        skill_body = ""
        skill_name: str | None = None
        if matches:
            skill_name = matches[0][0]
            try:
                skill_body = load_skill(skill_name)
            except SkillNotFoundError:
                skill_body = ""

        augmented_context = request.file_context
        if skill_body:
            augmented_context = (
                f"{request.file_context}\n\n"
                f"--- Bundled HDF5 skill ({skill_name}) ---\n"
                f"{skill_body}"
            ).strip()

        try:
            synthesis = self.agent(
                question=request.question,
                file_context=augmented_context,
            )
            analysis = str(getattr(synthesis, "analysis", "")).strip()
            recommendations = str(getattr(synthesis, "recommendations", "")).strip()
            if analysis:
                return ExpertResult(
                    analysis=analysis,
                    recommendations=recommendations,
                    source="dspy",
                    metadata={
                        "expert": "hdf5",
                        "mode": "skill_synthesis",
                        "consulted_skill": skill_name,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("HDF5Expert synthesis fallback failed: %s", exc)

        return ExpertResult(
            analysis=(
                "No concrete HDF5 file was named, and no skill matched the "
                "question strongly. I can give general HDF5 guidance but cannot "
                "make file-specific claims without a path."
            ),
            recommendations=(
                "Provide an HDF5 file path, or ask a focused question on one of "
                "the bundled topics (chunking, filters, parallel, VFD, VOL, "
                "SWMR, VDS, CF compliance, cloud-optimized HDF5)."
            ),
            source="fallback",
            metadata={"expert": "hdf5", "mode": "no_file_no_match"},
        )

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_object_path(question: str) -> str | None:
        """Find an HDF5 object path inside the question text.

        Recognized form: any token starting with ``/`` that isn't a file
        path (no ``.h5`` / ``.hdf5`` / ``.nc`` suffix).
        """
        for raw in question.split():
            token = raw.strip(",.()[]\"'")
            if not token.startswith("/"):
                continue
            lower = token.lower()
            if lower.endswith((".h5", ".hdf5", ".nc")):
                continue
            return token
        return None

    @staticmethod
    def _failure(
        filepath: str,
        error: dict[str, Any],
        runner: NativeToolRunner,
        *,
        mode: str,
    ) -> ExpertResult:
        return ExpertResult(
            analysis=f"Could not complete {mode} for {filepath}: {format_tool_error(error)}",
            recommendations=(
                "Verify the path, object_path, and tool contract. For "
                "missing-tool errors, install the missing binary (e.g. h5repack)."
            ),
            source="deterministic",
            tools=runner.observations,
            metadata={"expert": "hdf5", "mode": mode, "filepath": filepath},
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
        """Release tool executor resources."""
        self._tool_executor.close()

    @staticmethod
    def get_capabilities() -> dict[str, Any]:
        """Return routing metadata for AgentRegistry.

        Keywords are deliberately HDF5-specific (file extensions, VFDs,
        VOL connectors, SWMR/VDS, CF) so the existing keyword scorer
        picks this expert over DataExpert for HDF5-only questions while
        still ceding general data-format questions to DataExpert.
        """
        return {
            "name": "HDF5 Expert",
            "description": (
                "Specializes in HDF5 — layout, chunking, filters, VFDs, VOL "
                "connectors, parallel/SWMR/VDS, CF conventions, cloud-optimized "
                "variants, and visualization. ALSO the authority on ingesting "
                "HDF5 into clio-core / IOWarp (the CTE blob store via the CAE "
                "assimilator): whether bundling is worth it, when to consolidate "
                "datasets, read-vs-ingest and amortization trade-offs, and "
                "clio-core ingest performance. Route any HDF5 question — and any "
                "clio-core / IOWarp data-ingest or bundling question — here. "
                "Backed by a 25-skill in-process library."
            ),
            "keywords": [
                # Foundational
                "hdf5", "h5py", ".h5", ".hdf5", "h5repack",
                # Storage
                "chunk", "chunking", "rechunk", "chunk layout", "chunk size",
                "chunk cache", "chunk shape",
                # Compression / filters
                "gzip", "szip", "shuffle filter", "compression filter",
                "fletcher32", "scaleoffset", "n-bit", "nbit",
                # Parallel + concurrent
                "parallel hdf5", "mpi-io", "collective i/o", "swmr",
                "single writer multiple reader",
                # Virtual / view
                "virtual dataset", "vds", "region reference", "dimension scale",
                # VFD
                "ros3", "subfiling vfd", "core vfd", "onion vfd", "vol connector",
                "vol usage",
                # Cloud / service
                "hsds", "cloud-optimized hdf5", "hdf5 on s3", "byte-range request",
                # Types / structure
                "compound datatype", "vlen string", "h5t_compound", "map object",
                "h5m",
                # Workflow
                "cf compliance", "cf compliant", "cf convention", "cf conventions",
                "netcdf compliance", "netcdf4", "fair hdf5", "doi hdf5",
                # Action verbs
                "visualize", "plot dataset", "rechunk", "apply filter",
                "reclaim free space",
                # clio-core / IOWarp ingest (advisory)
                "clio-core", "clio core", "iowarp", "ingest", "ingest hdf5",
                "ingest into clio-core", "bundle", "context_bundle", "cte",
                "cae", "blob store", "data ingest", "should i use clio-core",
                "bundle vs read", "amortize ingest",
            ],
            "priority": 1,
        }
