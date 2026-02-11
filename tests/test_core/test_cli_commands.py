"""Tests for CLI command dispatch (/metrics, /compare, /rollback, /help).

Tests the ClioAgentCLI handle_command method with mocked agent, verifying
that optimizer commands integrate correctly without requiring LM Studio.
"""

import io
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.schema import Invocation, VariantRecord


class MockAgent:
    """Minimal ClioAgent mock for CLI testing."""

    def __init__(self, tmp_path):
        self.arc = ARCMemory(data_dir=str(tmp_path / "arc"))
        self.verbose = False
        self.data_expert = MagicMock()
        self.analysis_expert = MagicMock()
        self.visualization_expert = MagicMock()
        self.registry = MagicMock()
        self.registry.get_agent_count.return_value = 3
        self.registry.list_agents.return_value = ["data", "analysis", "visualization"]


def _make_cli(tmp_path):
    """Create a ClioAgentCLI with mocked internals."""
    from clio_agent.ui.cli import ClioAgentCLI

    with patch.object(ClioAgentCLI, "__init__", lambda self, **kwargs: None):
        cli = ClioAgentCLI.__new__(ClioAgentCLI)
        cli.console = Console(file=io.StringIO(), force_terminal=True)
        cli.verbose = False
        cli.history = []
        cli.agent = MockAgent(tmp_path)
        return cli


class TestHandleCommand:
    """Test handle_command method for CLI commands."""

    def test_help_command(self, tmp_path):
        """Test /help shows commands including new optimizer commands."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/help")
        assert result is True

        output = cli.console.file.getvalue()
        assert "/metrics" in output
        assert "/compare" in output
        assert "/rollback" in output

    def test_help_alias(self, tmp_path):
        """Test /h alias works."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/h")
        assert result is True

    def test_metrics_no_data(self, tmp_path):
        """Test /metrics with no invocation data shows message."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/metrics")
        assert result is True

        output = cli.console.file.getvalue()
        assert "No invocation data" in output

    def test_metrics_with_data(self, tmp_path):
        """Test /metrics with invocation data shows table."""
        cli = _make_cli(tmp_path)

        # Store some invocations in ARC
        import time

        for i in range(3):
            inv = Invocation(
                trace_id=f"trace-{i}",
                session_id="s1",
                parent_trace_id=None,
                agent_id="data",
                tier=2,
                source="native",
                started_at=time.time(),
                completed_at=time.time(),
                duration_ms=100.0 + i * 10,
                status="success",
                input={"question": f"q{i}"},
                output={"analysis": f"a{i}"},
                tools_called=[],
                nanoagents_spawned=[],
                performance={"success": True},
                storage_tier="warm",
            )
            cli.agent.arc.store_invocation(inv)

        result = cli.handle_command("/metrics")
        assert result is True

        output = cli.console.file.getvalue()
        assert "data" in output
        assert "100.0%" in output or "Success Rate" in output

    def test_compare_no_expert(self, tmp_path):
        """Test /compare without expert_id shows usage."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/compare")
        assert result is True

        output = cli.console.file.getvalue()
        assert "Usage" in output

    def test_compare_no_variants(self, tmp_path):
        """Test /compare with no variants shows message."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/compare data")
        assert result is True

        output = cli.console.file.getvalue()
        assert "No variants found" in output

    def test_compare_with_variants(self, tmp_path):
        """Test /compare with variants shows table."""
        cli = _make_cli(tmp_path)

        # Store variant records
        import time

        for i in range(2):
            record = VariantRecord(
                variant_id=f"data_v{i + 1}",
                agent_id="data",
                created_at=time.time(),
                training_examples=50,
                before_score=0.6,
                after_score=0.8,
                improvement_delta=0.2,
                p_value=0.01,
                is_significant=True,
                is_active=(i == 1),
                file_path=f"variants/data_v{i + 1}.json",
                dspy_version="3.1.3",
            )
            cli.agent.arc.store_variant_record(record)

        result = cli.handle_command("/compare data")
        assert result is True

        output = cli.console.file.getvalue()
        assert "data_v1" in output
        assert "data_v2" in output

    def test_rollback_no_expert(self, tmp_path):
        """Test /rollback without expert_id shows usage."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/rollback")
        assert result is True

        output = cli.console.file.getvalue()
        assert "Usage" in output

    def test_rollback_no_previous(self, tmp_path):
        """Test /rollback with no previous variant shows message."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/rollback data")
        assert result is True

        output = cli.console.file.getvalue()
        assert "No previous variant" in output

    def test_rollback_success(self, tmp_path):
        """Test /rollback with previous variant succeeds."""
        cli = _make_cli(tmp_path)

        import time

        # Create two variants, second is active
        for i in range(2):
            record = VariantRecord(
                variant_id=f"data_v{i + 1}",
                agent_id="data",
                created_at=time.time() - (1 - i),  # v1 older, v2 newer
                training_examples=50,
                before_score=0.6,
                after_score=0.8,
                improvement_delta=0.2,
                p_value=0.01,
                is_significant=True,
                is_active=(i == 1),
                file_path=f"variants/data_v{i + 1}.json",
                dspy_version="3.1.3",
            )
            cli.agent.arc.store_variant_record(record)

        # Mock load_variant since files don't exist
        with patch("clio_agent.optimizer.variants.VariantManager.load_variant"):
            result = cli.handle_command("/rollback data")

        assert result is True
        output = cli.console.file.getvalue()
        assert "Rolled back to variant" in output

    def test_unknown_command_returns_false(self, tmp_path):
        """Test unknown command returns False."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/nonexistent")
        assert result is False

    def test_verbose_toggle(self, tmp_path):
        """Test /verbose toggles verbose mode."""
        cli = _make_cli(tmp_path)
        assert cli.verbose is False
        cli.handle_command("/verbose")
        assert cli.verbose is True
        cli.handle_command("/verbose")
        assert cli.verbose is False

    def test_history_empty(self, tmp_path):
        """Test /history with no history shows message."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/history")
        assert result is True
        output = cli.console.file.getvalue()
        assert "No history" in output

    def test_clear_command(self, tmp_path):
        """Test /clear clears history."""
        cli = _make_cli(tmp_path)
        cli.history = [{"question": "test", "expert": "data", "answer": "answer"}]
        cli.handle_command("/clear")
        assert cli.history == []

    def test_experts_command(self, tmp_path):
        """Test /experts command shows expert table."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/experts")
        assert result is True
        output = cli.console.file.getvalue()
        assert "Available Experts" in output or "data" in output

    def test_registry_command(self, tmp_path):
        """Test /registry command shows registry info."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/registry")
        assert result is True
        output = cli.console.file.getvalue()
        assert "Registry" in output

    def test_memory_command(self, tmp_path):
        """Test /memory command shows ARC stats."""
        cli = _make_cli(tmp_path)
        # Mock get_arc_stats
        cli.agent.get_arc_stats = MagicMock(return_value={
            "hit_rate": 0.50,
            "hits": 10,
            "misses": 10,
            "size": 5,
            "capacity": 1000,
            "disk_reads": 3,
            "disk_writes": 7,
        })
        result = cli.handle_command("/memory")
        assert result is True
        output = cli.console.file.getvalue()
        assert "ARC Memory" in output

    def test_tools_command(self, tmp_path):
        """Test /tools command shows MCP tools."""
        cli = _make_cli(tmp_path)
        result = cli.handle_command("/tools")
        assert result is True
        output = cli.console.file.getvalue()
        assert "MCP Tools" in output or "Tool" in output

    def test_history_with_entries(self, tmp_path):
        """Test /history with entries shows them."""
        cli = _make_cli(tmp_path)
        cli.history = [
            {"question": "What is HDF5?", "expert": "data", "answer": "HDF5 is a file format"},
        ]
        result = cli.handle_command("/history")
        assert result is True
        output = cli.console.file.getvalue()
        assert "What is HDF5?" in output

    def test_ask_question(self, tmp_path):
        """Test ask_question dispatches to agent."""
        import dspy

        cli = _make_cli(tmp_path)
        mock_result = dspy.Prediction(
            answer="Test answer",
            selected_expert="data",
            duration_ms=100.0,
        )
        cli.agent.__call__ = MagicMock(return_value=mock_result)
        # Monkey-patch agent as callable
        cli.agent = MagicMock(return_value=mock_result)

        result = cli.ask_question("Test question")
        assert result["question"] == "Test question"
        assert result["expert"] == "data"
        assert result["answer"] == "Test answer"

    def test_print_banner(self, tmp_path):
        """Test print_banner renders without error."""
        cli = _make_cli(tmp_path)
        cli.print_banner()
        output = cli.console.file.getvalue()
        # Banner contains ASCII art with "___" patterns and project info
        assert "IOWarp" in output or "Scientific Computing" in output

    def test_memory_above_target(self, tmp_path):
        """Test /memory shows green when hit rate exceeds target."""
        cli = _make_cli(tmp_path)
        cli.agent.get_arc_stats = MagicMock(return_value={
            "hit_rate": 0.90,
            "hits": 90,
            "misses": 10,
            "size": 100,
            "capacity": 1000,
            "disk_reads": 5,
            "disk_writes": 10,
        })
        cli.handle_command("/memory")
        output = cli.console.file.getvalue()
        assert "exceeds target" in output


class TestConversationManager:
    """Tests for conversation_manager.py module."""

    def test_init(self):
        """Test ConversationManager initialization."""
        from clio_agent.conversation_manager import ConversationManager

        cm = ConversationManager(max_history_length=5)
        assert cm.max_history_length == 5
        assert cm.history_buffer == []

    def test_add_message(self):
        """Test adding messages to buffer."""
        from clio_agent.conversation_manager import ConversationManager

        cm = ConversationManager(max_history_length=3)
        cm.add_message("user", "Hello")
        cm.add_message("assistant", "Hi")
        assert len(cm.history_buffer) == 2

    def test_add_message_evicts_old(self):
        """Test buffer truncation when exceeding max length."""
        from clio_agent.conversation_manager import ConversationManager

        cm = ConversationManager(max_history_length=2)
        cm.add_message("user", "msg1")
        cm.add_message("assistant", "msg2")
        cm.add_message("user", "msg3")
        assert len(cm.history_buffer) == 2
        assert cm.history_buffer[0]["content"] == "msg2"

    def test_get_history(self):
        """Test get_history returns dspy.History."""
        from clio_agent.conversation_manager import ConversationManager

        cm = ConversationManager()
        cm.add_message("user", "test")
        history = cm.get_history()
        assert hasattr(history, "messages")


class TestCapabilityMatcher:
    """Tests for capability_matcher.py module."""

    def test_extract_keywords(self):
        """Test keyword extraction."""
        from clio_agent.registry.capability_matcher import CapabilityMatcher

        matcher = CapabilityMatcher()
        keywords = matcher.extract_keywords("How do I optimize HDF5 files?")
        assert "optimize" in keywords
        assert "hdf5" in keywords
        assert "files" in keywords
        assert "how" not in keywords  # stopword

    def test_match_query(self):
        """Test query matching against capabilities."""
        from clio_agent.registry.capability_matcher import CapabilityMatcher

        matcher = CapabilityMatcher()
        caps = {
            "data": {"keywords": ["hdf5", "compression", "chunking"]},
            "analysis": {"keywords": ["parquet", "statistics"]},
        }
        matches = matcher.match_query("optimize HDF5 compression", caps)
        assert len(matches) > 0
        assert matches[0][0] == "data"  # data expert should rank first

    def test_match_query_empty(self):
        """Test empty query keywords triggers warning."""
        import warnings

        from clio_agent.registry.capability_matcher import CapabilityMatcher

        matcher = CapabilityMatcher()
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = matcher.match_query("the a an", {"data": {"keywords": ["hdf5"]}})
            assert result == []
            assert len(w) == 1

    def test_match_no_overlap(self):
        """Test query with no keyword overlap returns empty."""
        from clio_agent.registry.capability_matcher import CapabilityMatcher

        matcher = CapabilityMatcher()
        caps = {"data": {"keywords": ["hdf5", "compression"]}}
        matches = matcher.match_query("weather forecast tomorrow", caps)
        assert matches == []

    def test_missing_keywords_in_capability(self):
        """Test agent with no keywords is skipped."""
        from clio_agent.registry.capability_matcher import CapabilityMatcher

        matcher = CapabilityMatcher()
        caps = {"data": {"name": "Data Expert"}}  # no 'keywords' key
        matches = matcher.match_query("optimize HDF5", caps)
        assert matches == []

    def test_multi_word_keywords(self):
        """Test multi-word keywords are expanded."""
        from clio_agent.registry.capability_matcher import CapabilityMatcher

        matcher = CapabilityMatcher()
        caps = {"hpc": {"keywords": ["parallel io", "mpi"]}}
        matches = matcher.match_query("parallel processing", caps)
        assert len(matches) > 0
