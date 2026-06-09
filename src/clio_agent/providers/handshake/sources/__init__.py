"""Context-source factory: resolve a model's limits from the layered cascade.

A provider's own metadata is authoritative; when it doesn't self-report a limit,
the handshake's ``enrich_capabilities`` step falls back to this factory, which
tries the sources in a **strict, fixed order** and returns the first hit with an
exact provenance string:

1. **models.dev** (``"models.dev"``) — the public catalog, fetched + cached with a
   TTL, offline-safe. Broadest coverage.
2. **db** (``"db"``) — the local model-limits database (:mod:`...sources.db`): a
   repo-shipped, lab-shareable JSON that is also **written back on discovery**, so
   models no public catalog lists yet are still known offline next time.

On a total miss the factory returns ``(None, "")`` and the caller keeps the
profile's limit unset. Provenance strings are exactly ``models.dev`` | ``db`` and
feed :attr:`ModelProfile.context_source`. Nothing here fetches at import time.
"""

from __future__ import annotations

from clio_agent.providers.handshake.sources import db
from clio_agent.providers.handshake.sources.models_dev import (
    lookup_models_dev,
    lookup_models_dev_output,
)

__all__ = [
    "db",
    "lookup_models_dev",
    "lookup_models_dev_output",
    "resolve_context",
    "resolve_output_limit",
]

#: Provenance string for the models.dev source.
SOURCE_MODELS_DEV = "models.dev"
#: Provenance string for the local DB source.
SOURCE_DB = "db"


def resolve_context(model_id: str, provider_kind: str) -> tuple[int | None, str]:
    """Resolve a context window for ``model_id`` via models.dev, then the local DB.

    Args:
        model_id: The raw model identifier (may include a ``vendor/`` prefix).
        provider_kind: The provider kind; accepted for call-site symmetry / a future
            source that disambiguates by backend (the current sources match on id).

    Returns:
        ``(context_window, source_name)`` where ``source_name`` is exactly
        ``"models.dev"`` or ``"db"`` on a hit, or ``(None, "")`` on a total miss.
    """
    if not (model_id or "").strip():
        return None, ""

    window = lookup_models_dev(model_id)
    if window is not None:
        return window, SOURCE_MODELS_DEV

    window = db.lookup_context(model_id)
    if window is not None:
        return window, SOURCE_DB

    return None, ""


def resolve_output_limit(model_id: str, provider_kind: str) -> int | None:
    """Resolve a model's maximum output tokens via models.dev, then the local DB."""
    if not (model_id or "").strip():
        return None
    output = lookup_models_dev_output(model_id)
    if output is not None:
        return output
    return db.lookup_output(model_id)
