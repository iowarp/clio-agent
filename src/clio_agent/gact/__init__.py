"""GACT v0.2 contract surface for CLIO.

This module implements the GACT v0.2 REST + SSE contract as a
FastAPI app that wraps ``ClioAgent``. It's a peer of
``clio_agent.ui.api`` (the v0.1-ish native CLIO API), not a
replacement — they can run side-by-side while the TUI integration
matures.

Exposed via the ``clio-agent-gact`` console script. See
``docs/tui/`` in this repo for the authoritative integration spec.
"""

from clio_agent.gact.app import build_app, main

__all__ = ["build_app", "main"]
