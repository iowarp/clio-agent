"""GACT v0.2 contract surface for CLIO.

This module implements the GACT v0.2 REST + SSE contract as a
FastAPI app that wraps ``ClioAgent``. It is CLIO's single HTTP front
door; the legacy ``clio_agent.ui.api`` REST server has been removed and
its ``clio-agent-api`` console script is now a deprecation shim.

Exposed via the ``clio-agent-gact`` console script. See
``docs/tui/`` in this repo for the authoritative integration spec.
"""

from clio_agent.gact.app import build_app, main

__all__ = ["build_app", "main"]
