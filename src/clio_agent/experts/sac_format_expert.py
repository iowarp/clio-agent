"""Nested SAC waveform format expert.

The SAC format expert owns SAC archive inspection, trace statistics, and
waveform plotting tool use. Top-level data, analysis, and visualization experts
may delegate to it, but SAC-specific tool semantics live here.
"""

from __future__ import annotations

from typing import Any

import dspy

from clio_agent.experts.native_tools import NativeToolRunner
from clio_agent.harness import (
    ExpertRequest,
    ExpertResult,
    format_tool_error,
    validate_tool_items,
    validate_tool_result,
)
from clio_agent.tools.execution import ToolExecutor, create_sync_tool_executor
from clio_agent.tools.gateway import gateway

SAC_ARCHIVE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "filepath": str,
    "sac_trace_count": int,
    "sample_members": list,
    "phases": list,
    "stations": list,
}

SAC_STATS_FIELDS: dict[str, type | tuple[type, ...]] = {
    "filepath": str,
    "sac_trace_count": int,
    "traces_analyzed": int,
    "traces": list,
}

SAC_TRACE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "member": str,
    "station": str,
    "phase": str,
    "npts": int,
    "delta_s": (int, float),
    "peak_abs": (int, float),
}

SAC_SUFFIXES = {".sac", ".tar", ".tgz", ".gz"}


class SACFormatExpert(dspy.Module):
    """Nested expert for SAC waveform archives and traces."""

    def __init__(self, tool_executor: ToolExecutor | None = None) -> None:
        """Initialize the nested SAC expert with SAC-scoped tools."""
        super().__init__()
        self._owns_executor = tool_executor is None
        self._tool_executor = tool_executor or create_sync_tool_executor(gateway)
        self._tools = [
            tool
            for tool in self._tool_executor.to_dspy_tools()
            if tool.name.startswith("sac_")
        ]

    def forward(self, question: str, file_context: str = "") -> dspy.Prediction:
        """Run the SAC expert and return a DSPy-compatible prediction."""
        result = self.run(ExpertRequest(question=question, file_context=file_context))
        return self._to_prediction(result)

    def run(self, request: ExpertRequest) -> ExpertResult:
        """Run SAC-specific inspection, analysis, or plotting."""
        filepath = self._first_sac_path(request.question, request.file_context)
        if not filepath:
            return ExpertResult(
                analysis="No SAC file or archive path was provided to the SAC format expert.",
                recommendations=(
                    "Pass a staged .sac/.tar/.tgz/.gz waveform path before requesting "
                    "SAC inspection, statistics, or plotting."
                ),
                source="deterministic",
                metadata={"expert": "sac_format", "format": "sac"},
            )

        q_lower = request.question.lower()
        if any(term in q_lower for term in ("plot", "visual", "chart", "figure", "graph")):
            return self.plot_traces(filepath)
        if any(
            term in q_lower
            for term in ("analyze", "analysis", "statistics", "stats", "peak", "sample")
        ):
            return self.compute_trace_statistics(filepath)
        return self.inspect_archive(filepath)

    def inspect_archive(self, filepath: str) -> ExpertResult:
        """Inspect SAC member structure in a staged file or archive."""
        runner = NativeToolRunner(self._tool_executor)
        result = runner.call(
            "sac_inspect_archive",
            {"filepath": filepath, "max_members": 12},
        )
        archive_valid = validate_tool_result(
            "sac_inspect_archive",
            result,
            SAC_ARCHIVE_FIELDS,
        )
        if not archive_valid.ok:
            assert archive_valid.error is not None
            runner.mark_validation_error("sac_inspect_archive", archive_valid.error)
            return ExpertResult(
                analysis=(
                    f"Could not inspect SAC archive {filepath}: "
                    f"{format_tool_error(archive_valid.error)}"
                ),
                recommendations="Verify the staged file is a SAC file or TAR archive.",
                source="deterministic",
                tools=runner.observations,
                metadata={"expert": "sac_format", "format": "sac", "filepath": filepath},
            )

        archive_data = archive_valid.data or {}
        samples = [str(member) for member in archive_data.get("sample_members", [])[:8]]
        phases = ", ".join(str(phase) for phase in archive_data.get("phases", [])[:8])
        stations = ", ".join(str(station) for station in archive_data.get("stations", [])[:8])
        analysis = (
            f"Inspected staged SAC waveform file {archive_data['filepath']}. "
            f"It contains {archive_data['sac_trace_count']} SAC traces."
        )
        if phases:
            analysis += f"\nPhases/groups: {phases}."
        if stations:
            analysis += f"\nSample stations: {stations}."
        if samples:
            analysis += "\nSample members:\n- " + "\n- ".join(samples)
        return ExpertResult(
            analysis=analysis,
            recommendations=(
                "Use sac_compute_trace_statistics for quantitative waveform checks and "
                "sac_plot_traces for representative trace plots."
            ),
            source="deterministic",
            tools=runner.observations,
            metadata={"expert": "sac_format", "format": "sac", "filepath": filepath},
        )

    def compute_trace_statistics(self, filepath: str) -> ExpertResult:
        """Compute waveform statistics for a staged SAC file or archive."""
        runner = NativeToolRunner(self._tool_executor)
        result = runner.call(
            "sac_compute_trace_statistics",
            {"filepath": filepath, "max_traces": 6},
        )
        stats_valid = validate_tool_result(
            "sac_compute_trace_statistics",
            result,
            SAC_STATS_FIELDS,
        )
        if not stats_valid.ok:
            assert stats_valid.error is not None
            runner.mark_validation_error("sac_compute_trace_statistics", stats_valid.error)
            return ExpertResult(
                analysis=(
                    f"Could not analyze SAC waveform file {filepath}: "
                    f"{format_tool_error(stats_valid.error)}"
                ),
                recommendations="Verify the staged file is a SAC file or TAR archive.",
                source="deterministic",
                tools=runner.observations,
                metadata={"expert": "sac_format", "format": "sac", "filepath": filepath},
            )

        stats_data = stats_valid.data or {}
        traces_valid = validate_tool_items(
            "sac_compute_trace_statistics",
            stats_data,
            "traces",
            SAC_TRACE_FIELDS,
        )
        if not traces_valid.ok:
            assert traces_valid.error is not None
            runner.mark_validation_error("sac_compute_trace_statistics", traces_valid.error)
            return ExpertResult(
                analysis=(
                    f"Could not validate SAC trace statistics for {filepath}: "
                    f"{format_tool_error(traces_valid.error)}"
                ),
                recommendations="Inspect the SAC tool contract before using this result.",
                source="deterministic",
                tools=runner.observations,
                metadata={"expert": "sac_format", "format": "sac", "filepath": filepath},
            )

        trace_lines = []
        for trace in stats_data.get("traces", [])[:6]:
            trace_lines.append(
                "- {station} {phase}: npts={npts}, delta_s={delta_s:.4g}, "
                "peak_abs={peak_abs:.4g}, member={member}".format(
                    station=trace.get("station", "unknown"),
                    phase=trace.get("phase", "unknown"),
                    npts=trace.get("npts", 0),
                    delta_s=float(trace.get("delta_s") or 0.0),
                    peak_abs=float(trace.get("peak_abs") or 0.0),
                    member=trace.get("member", ""),
                )
            )

        analysis = (
            f"Computed SAC waveform statistics for {filepath}. "
            f"The file exposes {stats_data['sac_trace_count']} SAC traces; "
            f"{stats_data['traces_analyzed']} traces were sampled for statistics.\n"
            + "\n".join(trace_lines)
        )
        return ExpertResult(
            analysis=analysis,
            recommendations=(
                "Use these trace-level statistics to choose representative stations/phases "
                "for visualization. For broader seismology semantics, add a dedicated "
                "ObsPy-backed reader."
            ),
            source="deterministic",
            tools=runner.observations,
            metadata={"expert": "sac_format", "format": "sac", "filepath": filepath},
        )

    def plot_traces(self, filepath: str) -> ExpertResult:
        """Create a representative SAC waveform plot."""
        runner = NativeToolRunner(self._tool_executor)
        result = runner.call("sac_plot_traces", {"filepath": filepath, "max_traces": 3})
        if isinstance(result, dict) and result.get("error"):
            return ExpertResult(
                analysis=(
                    "Could not create SAC waveform plot: "
                    f"{format_tool_error(result['error'])}"
                ),
                recommendations="Verify the staged file and SAC plotting tool contract.",
                source="deterministic",
                tools=runner.observations,
                metadata={"expert": "sac_format", "format": "sac", "filepath": filepath},
            )

        output_path = str(result.get("output_path") or "") if isinstance(result, dict) else ""
        traces_plotted = result.get("traces_plotted") if isinstance(result, dict) else None
        return ExpertResult(
            analysis=f"Plotted {traces_plotted} SAC waveform traces from {filepath}.",
            recommendations=f"Use the generated plot artifact at {output_path}.",
            source="deterministic",
            tools=runner.observations,
            metadata={
                "expert": "sac_format",
                "format": "sac",
                "filepath": filepath,
                "artifact_path": output_path,
            },
        )

    @staticmethod
    def _first_sac_path(question: str, file_context: str) -> str:
        """Extract the first SAC-compatible path from the prompt context."""
        from clio_agent.harness import extract_file_paths

        paths = extract_file_paths(question, file_context, SAC_SUFFIXES)
        return str(paths[0]) if paths else ""

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
        """Release tool execution resources if this expert owns them."""
        if self._owns_executor:
            self._tool_executor.close()

    @staticmethod
    def get_capabilities() -> dict[str, Any]:
        """Return SAC nested expert capability metadata."""
        return {
            "name": "SAC Format Expert",
            "description": (
                "Nested format expert for SAC waveform archive inspection, trace "
                "statistics, and representative waveform plots."
            ),
            "keywords": ["sac", "waveform", "trace", "seismology", "seismic"],
            "priority": 2,
        }


__all__ = ["SACFormatExpert", "SAC_SUFFIXES"]
