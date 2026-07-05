"""Deprecation shim for the removed ``clio-agent-api`` console script.

The legacy REST API server (``clio_agent.ui.api``) has been removed. CLIO now
has a single front door: the unified GACT server (``clio-agent-gact``), which
serves the versioned ``/v1`` API (including ``/v1/health``).

This module exists solely so the ``clio-agent-api`` entry point resolves to a
clear, structured pointer instead of an ``ImportError``. Invoking it prints the
migration guidance and exits with a non-zero status.
"""

from __future__ import annotations

import sys

_DEPRECATION_MESSAGE = (
    "clio-agent-api has been REMOVED.\n"
    "\n"
    "The legacy REST API server (clio_agent.ui.api) no longer exists. CLIO now\n"
    "has ONE front door: the unified GACT server.\n"
    "\n"
    "  Run instead:  clio-agent-gact --host 0.0.0.0 --port 8100\n"
    "\n"
    "It serves the versioned /v1 API, with health at:\n"
    "\n"
    "  GET /v1/health\n"
)


def main() -> None:
    """Print a structured deprecation pointer and exit non-zero.

    Raises:
        SystemExit: always, with code 2 — the ``clio-agent-api`` server has been
            removed in favor of ``clio-agent-gact``.
    """
    print(_DEPRECATION_MESSAGE, file=sys.stderr)
    raise SystemExit(2)


if __name__ == "__main__":
    main()
