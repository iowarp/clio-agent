"""
ClaudIO User Interfaces Module

Provides multiple interfaces for interacting with ClaudIO:
- CLI: Interactive command-line interface with Rich TUI
- API: FastAPI server with SSE support for web integration

Usage:
    # Command-line interface
    >>> from claudio.ui.cli import run_cli
    >>> run_cli()

    # API server
    >>> from claudio.ui.api import create_app
    >>> app = create_app()
    >>> # uvicorn claudio.ui.api:app --reload
"""

# TODO: Import when implemented
# from claudio.ui.cli import run_cli, ClaudIOCLI
# from claudio.ui.api import create_app

__all__ = [
    # "run_cli",
    # "ClaudIOCLI",
    # "create_app",
]
