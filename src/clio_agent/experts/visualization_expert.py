"""
ClioAgent Visualization Expert Module

Specializes in generating scientific data visualizations from tabular
datasets. Uses direct Python functions (matplotlib + pyarrow) wrapped
as dspy.Tool objects -- no MCP server needed.

The VisualizationExpert generates charts to disk and returns file paths.
Charts include histograms, bar charts, scatter plots, and summary dashboards.

Example:
    >>> from clio_agent.experts import VisualizationExpert
    >>> from clio_agent.config import setup_dspy
    >>>
    >>> lm = setup_dspy()
    >>> expert = VisualizationExpert(output_dir="/tmp/charts")
    >>> result = expert(
    ...     question="Plot the distribution of temperature",
    ...     file_context="data.parquet, 100 rows, weather sensor data"
    ... )
    >>> print(result.visualization_description)
    >>> print(result.file_path)
"""

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Optional

import dspy
import pyarrow.csv as pcsv
import pyarrow.parquet as pq

from clio_agent.harness import ToolObservation, normalize_tool_result, tool_result_ok
from clio_agent.signatures.visualization_sig import VisualizationExpertSignature
from clio_agent.tools.execution import notify_global_tool_observer
from clio_agent.tools.file_policy import (
    FilePolicyError,
    validate_non_empty_string,
    validate_positive_int,
    validate_read_path,
    validate_write_path,
)

logger = logging.getLogger(__name__)

# Matplotlib + the Agg backend pulls ~3-4s of import cost on Aurora's
# frameworks Python (beartype import hook + Lustre cold reads). The
# Visualization expert is rarely the first thing a user reaches for,
# so we defer the import until the first chart call. _plt() memoizes
# pyplot after running matplotlib.use("Agg") — call it from every
# chart function instead of binding ``plt`` at module level.
_plt_cache: Any = None


def _plt() -> Any:
    """Return matplotlib.pyplot, importing it (and forcing Agg) on
    first call. Subsequent calls hit the module cache so this is
    cheap on the hot path."""
    global _plt_cache  # noqa: PLW0603
    if _plt_cache is None:
        import matplotlib  # noqa: PLC0415

        matplotlib.use("Agg")  # headless before pyplot binds a backend
        import matplotlib.pyplot as plt  # noqa: PLC0415

        _plt_cache = plt
    return _plt_cache


def _load_table(filepath: str):
    """Load a Parquet or CSV file as a pyarrow Table.

    Args:
        filepath: Path to the data file (.parquet or .csv)

    Returns:
        pyarrow.Table

    Raises:
        ValueError: If file format is not supported
    """
    safe_path = validate_read_path(filepath)
    lower = str(safe_path).lower()
    if lower.endswith(".parquet"):
        return pq.read_table(safe_path)
    elif lower.endswith(".csv"):
        return pcsv.read_csv(safe_path)
    else:
        raise ValueError(f"Unsupported file format: {filepath}. Use .parquet or .csv")


def _resolve_output_path(output_path: str, default_filename: str) -> str:
    """Validate chart output path and return an absolute path string."""
    target = output_path or os.path.join(os.getcwd(), default_filename)
    return str(validate_write_path(target).resolve(strict=False))


def plot_histogram(filepath: str, column: str, bins: int = 30, output_path: str = "") -> str:
    """Create a histogram of a numeric column from a Parquet or CSV file.

    Loads the specified column, generates a matplotlib histogram with labeled
    axes and a descriptive title, saves to PNG, and returns the absolute path.

    Args:
        filepath: Path to the data file (.parquet or .csv)
        column: Name of the numeric column to plot
        bins: Number of histogram bins (default: 30)
        output_path: Output PNG path. If empty, auto-generated from column name.

    Returns:
        Absolute path to the saved PNG file, or error message string.
    """
    plt = _plt()
    try:
        validate_non_empty_string(column, field="column")
        validate_positive_int(bins, field="bins", max_value=1000)
        safe_output_path = _resolve_output_path(output_path, f"histogram_{column}.png")
        table = _load_table(filepath)
        if column not in table.column_names:
            return f"Error: Column '{column}' not found. Available: {table.column_names}"

        data = table.column(column).to_pylist()
        # Filter out None values
        data = [x for x in data if x is not None]
        if not data:
            return f"Error: Column '{column}' has no non-null values"

        fig, ax = plt.subplots(figsize=(8, 5))
        ax.hist(data, bins=bins, edgecolor="black", alpha=0.7)
        ax.set_xlabel(column)
        ax.set_ylabel("Count")
        ax.set_title(f"Distribution of {column}")
        ax.grid(axis="y", alpha=0.3)

        fig.savefig(safe_output_path, dpi=150, bbox_inches="tight")
        plt.close("all")

        return safe_output_path
    except FilePolicyError as e:
        plt.close("all")
        return e.to_text()
    except Exception as e:
        plt.close("all")
        return f"Error: {e}"


def plot_bar_chart(filepath: str, column: str, top_n: int = 10, output_path: str = "") -> str:
    """Create a horizontal bar chart of value counts for a column.

    Loads the specified column, computes value counts, plots the top N values
    as a horizontal bar chart, saves to PNG, and returns the absolute path.

    Args:
        filepath: Path to the data file (.parquet or .csv)
        column: Name of the column to plot value counts for
        top_n: Number of top categories to show (default: 10)
        output_path: Output PNG path. If empty, auto-generated from column name.

    Returns:
        Absolute path to the saved PNG file, or error message string.
    """
    plt = _plt()
    try:
        validate_non_empty_string(column, field="column")
        validate_positive_int(top_n, field="top_n", max_value=1000)
        safe_output_path = _resolve_output_path(output_path, f"bar_chart_{column}.png")
        table = _load_table(filepath)
        if column not in table.column_names:
            return f"Error: Column '{column}' not found. Available: {table.column_names}"

        data = table.column(column).to_pylist()
        # Count values, excluding None
        counts: dict[str, int] = {}
        for val in data:
            if val is not None:
                key = str(val)
                counts[key] = counts.get(key, 0) + 1

        if not counts:
            return f"Error: Column '{column}' has no non-null values"

        # Sort by count descending, take top_n
        sorted_items = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:top_n]
        labels = [item[0] for item in sorted_items]
        values = [item[1] for item in sorted_items]

        # Reverse for horizontal bar (top item at the top)
        labels = labels[::-1]
        values = values[::-1]

        fig, ax = plt.subplots(figsize=(8, max(3, len(labels) * 0.4 + 1)))
        ax.barh(labels, values, edgecolor="black", alpha=0.7)
        ax.set_xlabel("Count")
        ax.set_ylabel(column)
        ax.set_title(f"Top {min(top_n, len(labels))} values in {column}")
        ax.grid(axis="x", alpha=0.3)

        fig.savefig(safe_output_path, dpi=150, bbox_inches="tight")
        plt.close("all")

        return safe_output_path
    except FilePolicyError as e:
        plt.close("all")
        return e.to_text()
    except Exception as e:
        plt.close("all")
        return f"Error: {e}"


def plot_scatter(filepath: str, x_column: str, y_column: str, output_path: str = "") -> str:
    """Create a scatter plot of two numeric columns from a Parquet or CSV file.

    Loads both columns, creates a scatter plot with labeled axes and a
    descriptive title, saves to PNG, and returns the absolute path.

    Args:
        filepath: Path to the data file (.parquet or .csv)
        x_column: Name of the column for the X axis
        y_column: Name of the column for the Y axis
        output_path: Output PNG path. If empty, auto-generated from column names.

    Returns:
        Absolute path to the saved PNG file, or error message string.
    """
    plt = _plt()
    try:
        validate_non_empty_string(x_column, field="x_column")
        validate_non_empty_string(y_column, field="y_column")
        safe_output_path = _resolve_output_path(
            output_path,
            f"scatter_{x_column}_vs_{y_column}.png",
        )
        table = _load_table(filepath)
        for col in [x_column, y_column]:
            if col not in table.column_names:
                return f"Error: Column '{col}' not found. Available: {table.column_names}"

        x_data = table.column(x_column).to_pylist()
        y_data = table.column(y_column).to_pylist()

        # Filter paired None values
        pairs = [
            (x, y) for x, y in zip(x_data, y_data, strict=False) if x is not None and y is not None
        ]
        if not pairs:
            return f"Error: No valid data pairs for {x_column} vs {y_column}"

        x_vals, y_vals = zip(*pairs, strict=False)

        fig, ax = plt.subplots(figsize=(8, 6))
        ax.scatter(x_vals, y_vals, alpha=0.6, edgecolors="black", linewidths=0.3)
        ax.set_xlabel(x_column)
        ax.set_ylabel(y_column)
        ax.set_title(f"{y_column} vs {x_column}")
        ax.grid(alpha=0.3)

        fig.savefig(safe_output_path, dpi=150, bbox_inches="tight")
        plt.close("all")

        return safe_output_path
    except FilePolicyError as e:
        plt.close("all")
        return e.to_text()
    except Exception as e:
        plt.close("all")
        return f"Error: {e}"


def plot_summary(filepath: str, output_path: str = "") -> str:
    """Create a 2x2 summary dashboard of a dataset.

    Generates a multi-panel overview:
    - Top-left: Data types bar chart
    - Top-right: Null counts bar chart
    - Bottom-left: Numeric column distributions (histograms)
    - Bottom-right: Correlation heatmap for numeric columns

    Args:
        filepath: Path to the data file (.parquet or .csv)
        output_path: Output PNG path. If empty, auto-generated.

    Returns:
        Absolute path to the saved PNG file, or error message string.
    """
    plt = _plt()
    try:
        if not output_path:
            base = os.path.splitext(os.path.basename(filepath))[0]
            output_path = os.path.join(os.getcwd(), f"summary_{base}.png")
        safe_output_path = _resolve_output_path(output_path, "summary.png")
        table = _load_table(filepath)
        schema = table.schema
        col_names = table.column_names

        fig, axes = plt.subplots(2, 2, figsize=(14, 10))
        fig.suptitle(f"Dataset Summary: {os.path.basename(filepath)}", fontsize=14)

        # Top-left: Data types bar chart
        ax_types = axes[0, 0]
        type_counts: dict[str, int] = {}
        for field in schema:
            dtype_str = str(field.type)
            type_counts[dtype_str] = type_counts.get(dtype_str, 0) + 1
        if type_counts:
            labels = list(type_counts.keys())
            values = list(type_counts.values())
            ax_types.barh(labels, values, edgecolor="black", alpha=0.7, color="steelblue")
            ax_types.set_xlabel("Count")
            ax_types.set_title("Column Data Types")
        else:
            ax_types.text(0.5, 0.5, "No columns", ha="center", va="center")
            ax_types.set_title("Column Data Types")

        # Top-right: Null counts bar chart
        ax_nulls = axes[0, 1]
        null_counts = {}
        for name in col_names:
            col = table.column(name)
            null_count = col.null_count
            if null_count > 0:
                null_counts[name] = null_count
        if null_counts:
            labels = list(null_counts.keys())
            values = list(null_counts.values())
            ax_nulls.barh(labels, values, edgecolor="black", alpha=0.7, color="coral")
            ax_nulls.set_xlabel("Null Count")
            ax_nulls.set_title("Null Counts by Column")
        else:
            ax_nulls.text(0.5, 0.5, "No nulls found", ha="center", va="center")
            ax_nulls.set_title("Null Counts by Column")

        # Bottom-left: Numeric column distributions
        ax_hist = axes[1, 0]
        import pyarrow as pa

        numeric_types = (
            pa.int8(),
            pa.int16(),
            pa.int32(),
            pa.int64(),
            pa.uint8(),
            pa.uint16(),
            pa.uint32(),
            pa.uint64(),
            pa.float16(),
            pa.float32(),
            pa.float64(),
        )
        numeric_cols = [
            name
            for name in col_names
            if any(table.schema.field(name).type == t for t in numeric_types)
        ]
        if numeric_cols:
            for name in numeric_cols[:5]:  # Limit to 5 columns to avoid clutter
                data = [x for x in table.column(name).to_pylist() if x is not None]
                if data:
                    ax_hist.hist(data, bins=20, alpha=0.5, label=name, edgecolor="black")
            ax_hist.set_title("Numeric Distributions")
            ax_hist.set_ylabel("Count")
            ax_hist.legend(fontsize=8)
        else:
            ax_hist.text(0.5, 0.5, "No numeric columns", ha="center", va="center")
            ax_hist.set_title("Numeric Distributions")

        # Bottom-right: Correlation heatmap
        ax_corr = axes[1, 1]
        if len(numeric_cols) >= 2:
            import numpy as np

            num_data = {}
            for name in numeric_cols[:8]:  # Limit columns
                vals = table.column(name).to_pylist()
                num_data[name] = [x if x is not None else float("nan") for x in vals]

            col_labels = list(num_data.keys())
            matrix = np.array([num_data[c] for c in col_labels])

            # Compute correlation with nan handling
            with np.errstate(invalid="ignore"):
                corr = np.corrcoef(matrix)

            im = ax_corr.imshow(corr, cmap="coolwarm", vmin=-1, vmax=1)
            ax_corr.set_xticks(range(len(col_labels)))
            ax_corr.set_yticks(range(len(col_labels)))
            ax_corr.set_xticklabels(col_labels, rotation=45, ha="right", fontsize=8)
            ax_corr.set_yticklabels(col_labels, fontsize=8)
            ax_corr.set_title("Correlation Heatmap")
            fig.colorbar(im, ax=ax_corr, fraction=0.046, pad=0.04)
        else:
            ax_corr.text(0.5, 0.5, "Need 2+ numeric columns", ha="center", va="center")
            ax_corr.set_title("Correlation Heatmap")

        fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.96))

        fig.savefig(safe_output_path, dpi=150, bbox_inches="tight")
        plt.close("all")

        return safe_output_path
    except FilePolicyError as e:
        plt.close("all")
        return e.to_text()
    except Exception as e:
        plt.close("all")
        return f"Error: {e}"


class VisualizationExpert(dspy.Module):
    """Scientific visualization expert with ReAct + matplotlib chart tools.

    Uses direct Python functions (not MCP servers) wrapped as dspy.Tool
    objects for chart generation. Generates matplotlib charts saved to disk
    and returns file paths.

    Attributes:
        arc_memory: Optional ARC memory instance for caching
        output_dir: Directory for chart output files
        agent: DSPy ReAct module with chart tools

    Example:
        >>> expert = VisualizationExpert(output_dir="/tmp/charts")
        >>> print(f"Loaded {len(expert._tools)} tools")
        >>> result = expert(
        ...     question="Plot the distribution of temperature",
        ...     file_context="/path/to/data.parquet"
        ... )
        >>> print(result.file_path)
    """

    def __init__(self, arc_memory: Optional[Any] = None, output_dir: Optional[str] = None):
        """Initialize Visualization Expert with ReAct and chart tools.

        Args:
            arc_memory: Optional ARCMemory instance for caching
            output_dir: Directory for chart output (default: cwd)
        """
        super().__init__()
        self.arc_memory = arc_memory
        self.output_dir = output_dir or os.getcwd()
        self._tool_observations = threading.local()

        # Build dspy.Tool list from chart functions
        self._tools = [
            dspy.Tool(
                func=self._observed_tool("plot_histogram", plot_histogram),
                name="plot_histogram",
                desc=plot_histogram.__doc__,
                args={
                    "filepath": {
                        "type": "string",
                        "description": "Path to the data file (.parquet or .csv)",
                    },
                    "column": {
                        "type": "string",
                        "description": "Name of the numeric column to plot",
                    },
                    "bins": {
                        "type": "integer",
                        "description": "Number of histogram bins (default: 30)",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Output PNG path. If empty, auto-generated.",
                    },
                },
            ),
            dspy.Tool(
                func=self._observed_tool("plot_bar_chart", plot_bar_chart),
                name="plot_bar_chart",
                desc=plot_bar_chart.__doc__,
                args={
                    "filepath": {
                        "type": "string",
                        "description": "Path to the data file (.parquet or .csv)",
                    },
                    "column": {
                        "type": "string",
                        "description": "Name of the column to plot value counts for",
                    },
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top categories to show (default: 10)",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Output PNG path. If empty, auto-generated.",
                    },
                },
            ),
            dspy.Tool(
                func=self._observed_tool("plot_scatter", plot_scatter),
                name="plot_scatter",
                desc=plot_scatter.__doc__,
                args={
                    "filepath": {
                        "type": "string",
                        "description": "Path to the data file (.parquet or .csv)",
                    },
                    "x_column": {
                        "type": "string",
                        "description": "Name of the column for the X axis",
                    },
                    "y_column": {
                        "type": "string",
                        "description": "Name of the column for the Y axis",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Output PNG path. If empty, auto-generated.",
                    },
                },
            ),
            dspy.Tool(
                func=self._observed_tool("plot_summary", plot_summary),
                name="plot_summary",
                desc=plot_summary.__doc__,
                args={
                    "filepath": {
                        "type": "string",
                        "description": "Path to the data file (.parquet or .csv)",
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Output PNG path. If empty, auto-generated.",
                    },
                },
            ),
        ]

        logger.info(
            "VisualizationExpert initialized with %d tools: %s",
            len(self._tools),
            [t.name for t in self._tools],
        )

        # ReAct agent with chart tools
        self.agent = dspy.ReAct(
            VisualizationExpertSignature,
            tools=self._tools,
            max_iters=5,
        )

    def forward(self, question: str, file_context: str = "") -> dspy.Prediction:
        """Generate visualization using ReAct with chart tools.

        Args:
            question: User's question about data visualization
            file_context: File paths, column names, or context from prior analysis

        Returns:
            dspy.Prediction with visualization_description and file_path fields
        """
        self._set_tool_observations([])
        result = self.agent(question=question, file_context=file_context)
        try:
            result.tool_provenance = list(self._get_tool_observations())  # type: ignore[attr-defined]
        except Exception:
            pass
        finally:
            self._set_tool_observations([])
        return result

    def _observed_tool(self, name: str, func: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a local chart helper with CLIO tool provenance hooks."""

        def run(*args: Any, **kwargs: Any) -> Any:
            params = self._bind_tool_params(func, args, kwargs)
            start = time.time()
            notify_global_tool_observer(name, params, "started", None)
            try:
                raw_result = func(*args, **kwargs)
                result = normalize_tool_result(raw_result, tool=name)
            except Exception as exc:
                result = {"error": str(exc)}
                notify_global_tool_observer(name, params, "completed", repr(exc))
                self._record_tool_observation(name, params, result, start)
                raise
            completion_error = None if tool_result_ok(result) else repr(result["error"])
            notify_global_tool_observer(name, params, "completed", completion_error)
            self._record_tool_observation(name, params, result, start)
            return raw_result

        run.__name__ = getattr(func, "__name__", name)
        run.__doc__ = getattr(func, "__doc__", None)
        return run

    def _record_tool_observation(
        self,
        name: str,
        params: Mapping[str, Any],
        result: Any,
        start: float,
    ) -> None:
        observations = self._get_tool_observations()
        observations.append(
            ToolObservation(
                tool=name,
                params=dict(params),
                result=result,
                duration_ms=(time.time() - start) * 1000,
                ok=tool_result_ok(result),
            )
        )
        self._set_tool_observations(observations)

    def _get_tool_observations(self) -> list[ToolObservation]:
        observations = getattr(self._tool_observations, "value", None)
        if not isinstance(observations, list):
            observations = []
            self._set_tool_observations(observations)
        return observations

    def _set_tool_observations(self, observations: list[ToolObservation]) -> None:
        self._tool_observations.value = observations

    @staticmethod
    def _bind_tool_params(
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        import inspect

        try:
            bound = inspect.signature(func).bind_partial(*args, **kwargs)
            return dict(bound.arguments)
        except Exception:
            params: dict[str, Any] = {"args": list(args)}
            params.update(kwargs)
            return params

    @staticmethod
    def get_capabilities() -> dict[str, Any]:
        """Return expert capabilities for agent routing.

        Returns:
            Dictionary with name, description, keywords, priority.
            Used by ClioAgent to route questions to this expert.
        """
        return {
            "name": "Visualization Expert",
            "description": (
                "Specializes in generating scientific data visualizations: "
                "histograms, scatter plots, bar charts, and summary dashboards "
                "from tabular datasets (Parquet, CSV). Saves charts to disk as PNG."
            ),
            "keywords": [
                "visualization",
                "plot",
                "chart",
                "histogram",
                "scatter",
                "distribution",
                "bar chart",
                "graph",
            ],
            "priority": 3,
        }
