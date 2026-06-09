"""Context-source factory: resolve a model's context window from layered sources.

When a provider does not self-report a model's ``context_window`` (its ``"live"``
value is missing), the handshake's ``enrich_capabilities`` step falls back to this
factory. :func:`resolve_context` tries the sources in a **strict, fixed order**
and returns the first hit together with an exact provenance string:

1. **models.dev** (``"models.dev"``) — the public catalog, fetched and cached
   with a TTL, offline-safe. Most authoritative and broadest coverage.
2. **marketplace** (``"marketplace"``) — a human-curated DB (built-in seed plus an
   optional on-disk override) for models no public catalog lists yet.
3. **static** (``"static"``) — a tiny built-in table of slow-moving common models,
   so a fully offline clio can still size a request.

On a total miss the factory returns ``(None, "")`` and the caller keeps the
profile's window unset. Provenance strings are exactly ``models.dev`` |
``marketplace`` | ``static`` and feed :attr:`ModelProfile.context_source`.

Nothing here fetches at import time; the network is only touched lazily inside the
models.dev source on first use when its cache is stale.
"""

from __future__ import annotations

from clio_agent.providers.handshake.sources.marketplace import lookup_marketplace
from clio_agent.providers.handshake.sources.models_dev import lookup_models_dev
from clio_agent.providers.handshake.sources.static import lookup_static

__all__ = [
    "lookup_marketplace",
    "lookup_models_dev",
    "lookup_static",
    "resolve_context",
]

#: Provenance string for the models.dev source.
SOURCE_MODELS_DEV = "models.dev"
#: Provenance string for the curated marketplace source.
SOURCE_MARKETPLACE = "marketplace"
#: Provenance string for the static last-resort source.
SOURCE_STATIC = "static"


def resolve_context(model_id: str, provider_kind: str) -> tuple[int | None, str]:
    """Resolve a context window for ``model_id`` via the layered sources.

    Tries models.dev, then the marketplace DB, then the static table, in that
    strict order, returning at the first source that yields a value.

    Args:
        model_id: The raw model identifier (may include a ``vendor/`` prefix).
        provider_kind: The provider kind (e.g. ``"openai_compat"``, ``"argonne"``).
            Accepted for forward-compatibility and call-site symmetry; the current
            sources match on the id alone, but keeping it in the signature lets a
            future source disambiguate by backend without a breaking change.

    Returns:
        ``(context_window, source_name)`` where ``source_name`` is exactly one of
        ``"models.dev"``, ``"marketplace"`` or ``"static"`` on a hit, or
        ``(None, "")`` when no source knows the model.
    """
    if not (model_id or "").strip():
        return None, ""

    window = lookup_models_dev(model_id)
    if window is not None:
        return window, SOURCE_MODELS_DEV

    window = lookup_marketplace(model_id)
    if window is not None:
        return window, SOURCE_MARKETPLACE

    window = lookup_static(model_id)
    if window is not None:
        return window, SOURCE_STATIC

    return None, ""
