"""GACT v0.2 contract surface for CLIO.

This module implements the GACT v0.2 REST + SSE contract as a
FastAPI app that wraps ``ClioAgent``. It is CLIO's single HTTP front
door; the legacy ``clio_agent.ui.api`` REST server has been removed and
its ``clio-agent-api`` console script is now a deprecation shim.

Exposed via the ``clio-agent-gact`` console script. See
``docs/tui/`` in this repo for the authoritative integration spec.
"""

from typing import Any

__all__ = ["build_app", "main"]


def __getattr__(name: str) -> Any:
    """Load the HTTP application only when a public entry point is used.

    ``python -m clio_agent.gact`` imports this package before executing
    ``__main__``. Keeping this package initializer light lets the desktop
    bootstrap start its progress heartbeat before the larger FastAPI import
    graph is traversed.
    """

    if name in __all__:
        from clio_agent.gact import app  # noqa: PLC0415

        return getattr(app, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
