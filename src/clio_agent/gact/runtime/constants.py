"""Server-wide wire + limit constants for the GACT server (#714 decomposition).

These are the cross-concern scalar constants that the extracted route modules and
the assembly shell (:mod:`clio_agent.gact.app`) both reference. Folding them into a
leaf -- it imports only the config resolver -- lets the route modules read them
without importing back into ``app.py`` (which would violate the no-cycle
invariant). ``app.py`` re-exports them so existing
``from clio_agent.gact.app import <name>`` callers stay green.

* :data:`CONTRACT_VERSION` -- the GACT contract version advertised by
  ``GET /v1/capabilities``.
* :data:`GACT_BACKEND_VERSION` -- this backend build's version, surfaced by
  ``GET /v1/health`` / ``GET /v1/capabilities`` and the per-session SSE
  ``server.connected`` event.
* :data:`_CTX_MAX_BYTES` -- the per-attached-file inline cap context injection and
  the ``/v1/memory/stats`` retained-token estimate both respect.
"""

from __future__ import annotations

from importlib import metadata

import clio_agent
from clio_agent import conf

CONTRACT_VERSION = "0.2"


def _installed_clio_agent_version() -> str:
    """Return the installed package version exposed by the backend API."""

    try:
        return metadata.version("clio-agent")
    except metadata.PackageNotFoundError:
        return str(getattr(clio_agent, "__version__", "0.0.0"))


GACT_BACKEND_VERSION = _installed_clio_agent_version()

_CTX_MAX_BYTES = conf.resolve(
    "limits.context_inline_bytes",
    env="CLIO_CTX_MAX_BYTES",
    default=32 * 1024,  # 32 KB cap per attached file
    cast=conf.as_int,
)
