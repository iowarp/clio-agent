"""
ClioAgent User Interfaces Module

Provides multiple interfaces for interacting with ClioAgent:
- CLI: Interactive command-line interface with Rich TUI
- API: FastAPI server with SSE support for web integration

Usage:
    # Command-line interface
    >>> from clio_agent.ui.cli import run_cli
    >>> run_cli()

    # API server
    >>> from clio_agent.ui.api import create_app
    >>> app = create_app()
    >>> # uvicorn clio_agent.ui.api:app --reload
"""

# TODO: Import when implemented
# from clio_agent.ui.cli import run_cli, ClioAgentCLI
# from clio_agent.ui.api import create_app

__all__ = [
    # "run_cli",
    # "ClioAgentCLI",
    # "create_app",
]
