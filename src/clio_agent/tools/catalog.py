"""Tool ownership and visibility catalog for CLIO agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class ToolCatalogEntry:
    """Static ownership and routing metadata for one CLIO tool."""

    name: str
    owner: str
    tags: frozenset[str]
    visible_to: frozenset[str]
    planner_visible: bool = True


def _entry(
    name: str,
    owner: str,
    tags: Iterable[str],
    *,
    visible_to: Iterable[str] = (),
    planner_visible: bool = True,
) -> ToolCatalogEntry:
    scopes = set(visible_to)
    scopes.add(owner)
    if planner_visible:
        scopes.add("planner")
    return ToolCatalogEntry(
        name=name,
        owner=owner,
        tags=frozenset(tags),
        visible_to=frozenset(scopes),
        planner_visible=planner_visible,
    )


TOOL_CATALOG: dict[str, ToolCatalogEntry] = {
    "hdf5_list_datasets": _entry(
        "hdf5_list_datasets", "data", {"scientific-data", "hdf5", "inspect"}
    ),
    "hdf5_analyze_dataset": _entry(
        "hdf5_analyze_dataset", "data", {"scientific-data", "hdf5", "inspect"}
    ),
    "hdf5_check_compression": _entry(
        "hdf5_check_compression", "data", {"scientific-data", "hdf5", "compression"}
    ),
    "hdf5_optimize_chunking": _entry(
        "hdf5_optimize_chunking", "data", {"scientific-data", "hdf5", "chunking"}
    ),
    "hdf5_analyze_file": _entry(
        "hdf5_analyze_file", "data", {"scientific-data", "hdf5", "inspect"}
    ),
    "adios_inspect_file": _entry(
        "adios_inspect_file", "data", {"scientific-data", "adios", "bp5", "inspect"}
    ),
    "adios_inspect_variables": _entry(
        "adios_inspect_variables", "data", {"scientific-data", "adios", "bp5", "variables"}
    ),
    "adios_inspect_profiling": _entry(
        "adios_inspect_profiling", "data", {"scientific-data", "adios", "bp5", "profiling"}
    ),
    "parquet_analyze_schema": _entry(
        "parquet_analyze_schema", "analysis", {"tabular", "parquet", "schema"}
    ),
    "parquet_query_data": _entry("parquet_query_data", "analysis", {"tabular", "parquet", "query"}),
    "parquet_compute_statistics": _entry(
        "parquet_compute_statistics", "analysis", {"tabular", "parquet", "statistics"}
    ),
    "csv_read_table": _entry("csv_read_table", "analysis", {"tabular", "csv", "schema"}),
    "ndp_list_organizations": _entry(
        "ndp_list_organizations",
        "ndp_catalog",
        {"catalog", "ndp", "discovery"},
        visible_to={"data"},
    ),
    "ndp_search_datasets": _entry(
        "ndp_search_datasets",
        "ndp_catalog",
        {"catalog", "ndp", "search"},
        visible_to={"data"},
    ),
    "ndp_get_dataset_details": _entry(
        "ndp_get_dataset_details",
        "ndp_catalog",
        {"catalog", "ndp", "metadata"},
        visible_to={"data"},
    ),
    "ndp_stage_resource": _entry(
        "ndp_stage_resource",
        "ndp_catalog",
        {"catalog", "ndp", "download", "staging"},
        visible_to={"data"},
    ),
    "sac_inspect_archive": _entry(
        "sac_inspect_archive",
        "sac_format",
        {"scientific-data", "seismic", "sac", "inspect"},
        visible_to={"data"},
    ),
    "sac_compute_trace_statistics": _entry(
        "sac_compute_trace_statistics",
        "sac_format",
        {"seismic", "sac", "statistics", "waveform"},
        visible_to={"analysis"},
    ),
    "sac_plot_traces": _entry(
        "sac_plot_traces",
        "sac_format",
        {"seismic", "sac", "waveform", "visualization", "plot"},
        visible_to={"visualization"},
    ),
    "genomics_inspect_fasta": _entry(
        "genomics_inspect_fasta",
        "genomics",
        {"genomics", "fasta", "sequence", "inspect"},
    ),
    "genomics_summarize_vcf": _entry(
        "genomics_summarize_vcf",
        "genomics",
        {"genomics", "vcf", "variants", "sequence", "analysis"},
    ),
    "materials_inspect_cif": _entry(
        "materials_inspect_cif",
        "materials",
        {"materials", "crystallography", "cif", "structure", "inspect"},
    ),
    "geospatial_inspect_geojson": _entry(
        "geospatial_inspect_geojson",
        "geospatial",
        {"geospatial", "geojson", "spatial", "geometry", "inspect"},
    ),
    "imaging_inspect_png": _entry(
        "imaging_inspect_png",
        "imaging",
        {"imaging", "microscopy", "png", "image", "inspect"},
    ),
    "mass_spec_inspect_mzml": _entry(
        "mass_spec_inspect_mzml",
        "mass_spec",
        {"mass-spectrometry", "mzml", "proteomics", "spectra", "inspect"},
    ),
    "plot_histogram": _entry(
        "plot_histogram", "visualization", {"visualization", "plot", "histogram"}
    ),
    "plot_bar_chart": _entry(
        "plot_bar_chart", "visualization", {"visualization", "plot", "bar-chart"}
    ),
    "plot_scatter": _entry("plot_scatter", "visualization", {"visualization", "plot", "scatter"}),
    "plot_summary": _entry("plot_summary", "visualization", {"visualization", "plot", "summary"}),
    "shell_bash": _entry(
        "shell_bash",
        "utility",
        {"utility", "shell", "local", "diagnostic"},
        visible_to={"chat"},
    ),
    "fs_propose_edit": _entry(
        "fs_propose_edit", "utility", {"workspace", "edit", "diff", "proposal"}
    ),
    "fs_read_file": _entry(
        "fs_read_file", "workspace", {"workspace", "read"}, planner_visible=False
    ),
    "fs_apply_edit_write": _entry(
        "fs_apply_edit_write", "workspace", {"workspace", "write"}, planner_visible=False
    ),
}


def get_tool_entry(tool_name: str) -> ToolCatalogEntry | None:
    """Return catalog metadata for a tool name, if CLIO owns it."""

    return TOOL_CATALOG.get(tool_name)


def tool_owner(tool_name: str) -> str:
    """Return the owning expert id for a tool, or an empty string if unknown."""

    entry = get_tool_entry(tool_name)
    return entry.owner if entry else ""


def tool_tags(tool_name: str) -> frozenset[str]:
    """Return tags for a tool, or an empty set if unknown."""

    entry = get_tool_entry(tool_name)
    return entry.tags if entry else frozenset()


def tool_visible_to(tool_name: str, scope: str) -> bool:
    """Return whether a tool should be visible to an agent or planner scope."""

    entry = get_tool_entry(tool_name)
    return bool(entry and scope in entry.visible_to)


def tool_visible_scopes(tool_name: str) -> list[str]:
    """Return sorted agent/planner scopes allowed to see a tool."""

    entry = get_tool_entry(tool_name)
    return sorted(entry.visible_to) if entry else []


def tool_names_for_owner(owner: str, *, planner_visible_only: bool = True) -> list[str]:
    """Return catalog tool names owned by an expert id."""

    names = [
        entry.name
        for entry in TOOL_CATALOG.values()
        if entry.owner == owner and (entry.planner_visible or not planner_visible_only)
    ]
    return sorted(names)


def filter_tool_names_for_scope(tool_names: Iterable[str], scope: str) -> list[str]:
    """Filter tool names to those visible to one agent/planner scope."""

    return sorted(name for name in tool_names if tool_visible_to(name, scope))
