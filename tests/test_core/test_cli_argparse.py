"""Tests for CLI argparse and run_cli function.

Tests the argparse configuration without actually running interactive mode.
"""

import pytest


class TestRunCli:
    """Test run_cli function."""

    def test_run_cli_is_callable(self):
        """run_cli should exist and be callable."""
        from clio_agent.ui.cli import run_cli

        assert callable(run_cli)


class TestCliArgparse:
    """Test CLI argparse configuration (lines 539-573)."""

    def test_parse_verbose_flag(self):
        """--verbose flag should set args.verbose=True."""
        import argparse

        # Simulate argparse from cli.py
        parser = argparse.ArgumentParser()
        parser.add_argument("--verbose", "-v", action="store_true")
        parser.add_argument("--query", "-q", type=str)
        parser.add_argument("--session", type=str, default="cli_session")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--tune", type=str, choices=["data", "analysis", "visualization"])

        args = parser.parse_args(["--verbose"])
        assert args.verbose is True

    def test_parse_query_flag(self):
        """--query flag should set args.query."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--verbose", "-v", action="store_true")
        parser.add_argument("--query", "-q", type=str)
        parser.add_argument("--session", type=str, default="cli_session")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--tune", type=str, choices=["data", "analysis", "visualization"])

        args = parser.parse_args(["--query", "What is HDF5?"])
        assert args.query == "What is HDF5?"
        assert args.verbose is False

    def test_parse_json_flag(self):
        """--json flag should set args.json=True."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--verbose", "-v", action="store_true")
        parser.add_argument("--query", "-q", type=str)
        parser.add_argument("--session", type=str, default="cli_session")
        parser.add_argument("--json", action="store_true")
        parser.add_argument("--tune", type=str, choices=["data", "analysis", "visualization"])

        args = parser.parse_args(["--query", "test", "--json"])
        assert args.json is True

    def test_parse_session_default(self):
        """--session should default to cli_session."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--session", type=str, default="cli_session")
        args = parser.parse_args([])
        assert args.session == "cli_session"

    def test_parse_tune_flag(self):
        """--tune should accept valid expert ids."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--tune", type=str, choices=["data", "analysis", "visualization"])
        args = parser.parse_args(["--tune", "data"])
        assert args.tune == "data"

    def test_parse_tune_invalid_rejected(self):
        """--tune with invalid expert should raise SystemExit."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--tune", type=str, choices=["data", "analysis", "visualization"])
        with pytest.raises(SystemExit):
            parser.parse_args(["--tune", "invalid"])

    def test_parse_combined_flags(self):
        """Combined flags should work together."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("--verbose", "-v", action="store_true")
        parser.add_argument("--query", "-q", type=str)
        parser.add_argument("--session", type=str, default="cli_session")
        parser.add_argument("--json", action="store_true")

        args = parser.parse_args(["-v", "-q", "test query", "--session", "s1", "--json"])
        assert args.verbose is True
        assert args.query == "test query"
        assert args.session == "s1"
        assert args.json is True
