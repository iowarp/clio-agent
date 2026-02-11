"""
Tests for Visualization Expert module.

Tests VisualizationExpert initialization, tool loading, chart generation,
capabilities, and signature. Does not require LM Studio (no forward() tests
that call real LMs). Tool functions are tested directly.
"""

import inspect
import os
from unittest.mock import Mock

from clio_agent.experts.visualization_expert import (
    VisualizationExpert,
    plot_bar_chart,
    plot_histogram,
    plot_scatter,
    plot_summary,
)


class TestVisualizationExpertSignature:
    """Test the VisualizationExpertSignature prompt."""

    def test_visualization_signature_has_domain_prompt(self):
        """Test signature docstring is a substantial domain prompt (500+ words)."""
        from clio_agent.signatures.visualization_sig import VisualizationExpertSignature

        doc = VisualizationExpertSignature.__doc__
        assert doc is not None
        word_count = len(doc.split())
        assert word_count >= 500, f"Signature prompt is only {word_count} words, need 500+"

    def test_signature_fields(self):
        """Test signature has the expected input/output fields."""
        from clio_agent.signatures.visualization_sig import VisualizationExpertSignature

        assert "question" in VisualizationExpertSignature.input_fields
        assert "file_context" in VisualizationExpertSignature.input_fields
        assert "visualization_description" in VisualizationExpertSignature.output_fields
        assert "file_path" in VisualizationExpertSignature.output_fields


class TestVisualizationExpert:
    """Test Visualization Expert functionality."""

    def test_visualization_expert_loads_tools(self):
        """Test expert has exactly 4 chart tools."""
        expert = VisualizationExpert()
        assert len(expert._tools) == 4

    def test_visualization_expert_tool_names(self):
        """Test expert has the expected tool names."""
        expert = VisualizationExpert()
        tool_names = [t.name for t in expert._tools]
        assert "plot_histogram" in tool_names
        assert "plot_bar_chart" in tool_names
        assert "plot_scatter" in tool_names
        assert "plot_summary" in tool_names

    def test_visualization_expert_has_react_agent(self):
        """Test expert uses ReAct."""
        expert = VisualizationExpert()
        assert hasattr(expert.agent, "tools")
        agent_type = type(expert.agent).__name__
        assert "ReAct" in agent_type

    def test_visualization_expert_capabilities(self):
        """Test expert capabilities contain visualization-related keywords."""
        caps = VisualizationExpert.get_capabilities()
        keywords = caps["keywords"]
        assert "visualization" in keywords
        assert "plot" in keywords
        assert "chart" in keywords
        assert "histogram" in keywords
        assert "scatter" in keywords
        assert "distribution" in keywords
        assert "bar chart" in keywords
        assert "graph" in keywords
        assert caps["name"] == "Visualization Expert"
        assert caps["priority"] == 3

    def test_visualization_expert_forward_signature(self):
        """Test forward method accepts question and file_context parameters."""
        expert = VisualizationExpert()
        sig = inspect.signature(expert.forward)
        params = list(sig.parameters.keys())
        assert "question" in params
        assert "file_context" in params

    def test_visualization_expert_with_arc_memory(self):
        """Test expert with ARC memory integration."""
        mock_arc = Mock()
        expert = VisualizationExpert(arc_memory=mock_arc)
        assert expert.arc_memory is mock_arc

    def test_visualization_expert_output_dir(self):
        """Test expert respects custom output directory."""
        expert = VisualizationExpert(output_dir="/tmp/test_charts")
        assert expert.output_dir == "/tmp/test_charts"

    def test_visualization_expert_default_output_dir(self):
        """Test expert defaults to cwd for output."""
        expert = VisualizationExpert()
        assert expert.output_dir == os.getcwd()


class TestPlotHistogram:
    """Test plot_histogram function directly."""

    def test_plot_histogram_creates_file(self, sample_parquet, tmp_path):
        """Test histogram creates a PNG file on disk."""
        output = str(tmp_path / "hist_temp.png")
        result = plot_histogram(sample_parquet, "temperature", output_path=output)
        assert result == os.path.abspath(output)
        assert os.path.exists(result)
        assert os.path.getsize(result) > 0

    def test_plot_histogram_bad_file(self, tmp_path):
        """Test histogram returns error string for missing file."""
        result = plot_histogram("/nonexistent/file.parquet", "temperature")
        assert result.startswith("Error:")

    def test_plot_histogram_bad_column(self, sample_parquet, tmp_path):
        """Test histogram returns error for nonexistent column."""
        output = str(tmp_path / "hist_bad.png")
        result = plot_histogram(sample_parquet, "nonexistent_column", output_path=output)
        assert result.startswith("Error:")
        assert "nonexistent_column" in result


class TestPlotBarChart:
    """Test plot_bar_chart function directly."""

    def test_plot_bar_chart_creates_file(self, sample_parquet, tmp_path):
        """Test bar chart creates a PNG file on disk."""
        output = str(tmp_path / "bar_city.png")
        result = plot_bar_chart(sample_parquet, "city", output_path=output)
        assert result == os.path.abspath(output)
        assert os.path.exists(result)
        assert os.path.getsize(result) > 0

    def test_plot_bar_chart_bad_column(self, sample_parquet, tmp_path):
        """Test bar chart returns error for nonexistent column."""
        output = str(tmp_path / "bar_bad.png")
        result = plot_bar_chart(sample_parquet, "nonexistent", output_path=output)
        assert result.startswith("Error:")


class TestPlotScatter:
    """Test plot_scatter function directly."""

    def test_plot_scatter_creates_file(self, sample_parquet, tmp_path):
        """Test scatter plot creates a PNG file on disk."""
        output = str(tmp_path / "scatter.png")
        result = plot_scatter(sample_parquet, "id", "temperature", output_path=output)
        assert result == os.path.abspath(output)
        assert os.path.exists(result)
        assert os.path.getsize(result) > 0

    def test_plot_scatter_bad_column(self, sample_parquet, tmp_path):
        """Test scatter returns error for nonexistent column."""
        output = str(tmp_path / "scatter_bad.png")
        result = plot_scatter(sample_parquet, "id", "nonexistent", output_path=output)
        assert result.startswith("Error:")


class TestPlotSummary:
    """Test plot_summary function directly."""

    def test_plot_summary_creates_file(self, sample_parquet, tmp_path):
        """Test summary dashboard creates a PNG file on disk."""
        output = str(tmp_path / "summary.png")
        result = plot_summary(sample_parquet, output_path=output)
        assert result == os.path.abspath(output)
        assert os.path.exists(result)
        assert os.path.getsize(result) > 0

    def test_plot_summary_bad_file(self):
        """Test summary returns error for missing file."""
        result = plot_summary("/nonexistent/file.parquet")
        assert result.startswith("Error:")
