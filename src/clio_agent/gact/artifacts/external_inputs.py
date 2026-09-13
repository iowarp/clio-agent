"""Explicit external-file consumption contracts for artifact provenance."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clio_agent.tools.file_policy import _is_relative_to

logger = logging.getLogger(__name__)

# External paths are weaker evidence than contained, hashed workspace inputs.
# Only arguments whose exact tool contract consumes file bytes belong here.
_EXTERNAL_FILE_INPUT_ARGS: dict[str, frozenset[str]] = {
    "fs_read_file": frozenset({"filepath"}),
    "fs_propose_edit": frozenset({"filepath"}),
    "pandas_load_data": frozenset({"file_path"}),
    "pandas_statistical_summary": frozenset({"file_path"}),
    "pandas_correlation_analysis": frozenset({"file_path"}),
    "pandas_hypothesis_testing": frozenset({"file_path"}),
    "pandas_handle_missing_data": frozenset({"file_path"}),
    "pandas_clean_data": frozenset({"file_path"}),
    "pandas_groupby_operations": frozenset({"file_path"}),
    "pandas_merge_datasets": frozenset({"left_file", "right_file"}),
    "pandas_pivot_table": frozenset({"file_path"}),
    "pandas_time_series_operations": frozenset({"file_path"}),
    "pandas_validate_data": frozenset({"file_path"}),
    "pandas_filter_data": frozenset({"file_path"}),
    "pandas_optimize_memory": frozenset({"file_path"}),
    "pandas_profile_data": frozenset({"file_path"}),
    "pandas_profile_csv": frozenset({"data_path"}),
    "geo_render_feature_map": frozenset({"data_path"}),
    "geo_points_in_polygons": frozenset({"points_path", "polygons_path"}),
    "geo_bounding_box": frozenset({"data_path"}),
    "geo_filter_points_by_radius": frozenset({"data_path"}),
    "plot_line_plot": frozenset({"file_path"}),
    "plot_bar_plot": frozenset({"file_path"}),
    "plot_scatter_plot": frozenset({"file_path"}),
    "plot_histogram_plot": frozenset({"file_path"}),
    "plot_heatmap_plot": frozenset({"file_path"}),
    "plot_data_info": frozenset({"file_path"}),
}

_NON_CONSUMING_PATH_TOOLS = frozenset(
    {
        "fs_apply_edit_write",
        "fs_list_directory",
        "ndp_list_organizations",
        "ndp_search_datasets",
        "ndp_get_dataset_details",
        "ndp_stage_resource",
    }
)
_NON_WRITING_FILE_CONSUMERS = frozenset(
    {
        "fs_read_file",
        "fs_propose_edit",
        "pandas_load_data",
        "pandas_statistical_summary",
        "pandas_correlation_analysis",
        "pandas_hypothesis_testing",
        "pandas_groupby_operations",
        "pandas_pivot_table",
        "pandas_time_series_operations",
        "pandas_validate_data",
        "pandas_optimize_memory",
        "pandas_profile_data",
        "pandas_profile_csv",
        "geo_bounding_box",
        "geo_filter_points_by_radius",
        "plot_data_info",
    }
)
_PATH_SCHEMA_ARG_NAMES = frozenset(
    {
        "path",
        "filepath",
        "file_path",
        "data_path",
        "input_path",
        "source_path",
        "left_file",
        "right_file",
        "points_path",
        "polygons_path",
    }
)


def external_file_input_args(tool_name: str) -> frozenset[str]:
    """Return file-consuming argument names for an exact tool contract."""

    return _EXTERNAL_FILE_INPUT_ARGS.get(tool_name, frozenset())


def external_file_arg_was_consumed(tool_name: str, candidate: Path) -> bool:
    """Resolve conditional file consumption without reading or hashing bytes."""

    if tool_name != "fs_propose_edit":
        return True
    try:
        return candidate.is_file()
    except (OSError, ValueError):
        return False


def declared_consumed_file_paths(tool_name: str, args: Mapping[str, Any]) -> set[str]:
    """Return normalized absolute paths declared as consumed by a tool."""

    paths: set[str] = set()
    for arg_name in external_file_input_args(tool_name):
        value = args.get(arg_name)
        values = value if isinstance(value, (list, tuple)) else (value,)
        for raw in values:
            if not isinstance(raw, str) or not raw.strip():
                continue
            candidate = Path(raw)
            if not candidate.is_absolute() or not external_file_arg_was_consumed(
                tool_name, candidate
            ):
                continue
            try:
                paths.add(str(candidate.expanduser().resolve(strict=False)))
            except (OSError, ValueError):
                continue
    return paths


def is_non_writing_file_consumer(tool_name: str) -> bool:
    """Return whether the consumer contract does not write file outputs."""

    return tool_name in _NON_WRITING_FILE_CONSUMERS


def is_known_non_consuming_path_tool(tool_name: str) -> bool:
    """Return whether path-shaped arguments are known not to consume bytes."""

    return tool_name in _NON_CONSUMING_PATH_TOOLS


def is_path_schema_arg(arg_name: str) -> bool:
    """Return whether an argument name represents a filesystem path."""

    return arg_name in _PATH_SCHEMA_ARG_NAMES


def consumed_input_echo(
    channel: str,
    root: Path | None,
    path: Path,
    tool_name: str,
    label: str,
    raw_path: str,
    consumed_paths: set[str],
) -> bool:
    """Return and record when a declared result only echoes an external input."""

    try:
        normalized = str(path.expanduser().resolve(strict=False))
    except (OSError, ValueError):
        normalized = str(path)
    outside = root is not None and not _is_relative_to(Path(normalized), root)
    detected = (
        channel == "result"
        and outside
        and normalized in consumed_paths
        and is_non_writing_file_consumer(tool_name)
    )
    if detected:
        logger.info(
            "artifact mint skipped reason=consumed_input_echo tool=%s %s=%s path=%s",
            tool_name,
            channel,
            label,
            raw_path,
        )
    return detected
