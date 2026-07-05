"""
ClioAgent User Interfaces Module

Provides the local command-line surface for interacting with ClioAgent. The
programmatic front door is the unified GACT server (``clio-agent-gact`` /
``clio_agent.gact.app``), which serves the versioned ``/v1`` API.

Usage:
    # Command-line interface
    >>> from clio_agent.ui.cli import run_cli
    >>> run_cli()

    # HTTP API server (unified GACT front door)
    >>> # clio-agent-gact --host 0.0.0.0 --port 8100
"""

# TODO: Import when implemented
# from clio_agent.ui.cli import run_cli, ClioAgentCLI

__all__: list[str] = [
    # "run_cli",
    # "ClioAgentCLI",
]
