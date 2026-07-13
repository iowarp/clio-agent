"""Context-source factory: resolve a model's limits from the layered cascade.

The full cascade is **provider-self-reported → models.dev → litellm catalog →
local DB**. A provider's own metadata is authoritative; when it doesn't
self-report a limit, the handshake's ``enrich_capabilities`` step falls back to
this factory, which tries the remaining sources in a **strict, fixed order** and
returns the first hit with an exact provenance string:

1. **models.dev** (``"models.dev"``) — the public catalog, fetched + cached with a
   TTL, offline-safe. Broadest coverage.
2. **litellm** (``"litellm"``) — the bundled LiteLLM model-cost catalog, an
   offline in-process lookup for models models.dev doesn't list.
3. **db** (``"db"``) — the local model-limits database (:mod:`...sources.db`): a
   repo-shipped, lab-shareable JSON that is also **written back on discovery**, so
   models no public catalog lists yet are still known offline next time.

On a total miss the factory returns ``(None, "")`` and the caller keeps the
profile's limit unset. Provenance strings are exactly ``models.dev`` | ``litellm``
| ``db`` and feed :attr:`ModelProfile.context_source`. Nothing here fetches at
import time.
"""

from __future__ import annotations

from clio_agent.providers.handshake.sources import db
from clio_agent.providers.handshake.sources.litellm_catalog import (
    lookup_litellm_context,
    lookup_litellm_output,
)
from clio_agent.providers.handshake.sources.models_dev import (
    lookup_models_dev,
    lookup_models_dev_output,
)

__all__ = [
    "db",
    "lookup_litellm_context",
    "lookup_litellm_output",
    "lookup_models_dev",
    "lookup_models_dev_output",
    "resolve_context",
    "resolve_output_limit",
]

#: Provenance string for the models.dev source.
SOURCE_MODELS_DEV = "models.dev"
#: Provenance string for the LiteLLM catalog source.
SOURCE_LITELLM = "litellm"
#: Provenance string for the local DB source.
SOURCE_DB = "db"


def resolve_context(model_id: str, provider_kind: str) -> tuple[int | None, str]:
    """Resolve a context window via models.dev, then LiteLLM, then the local DB.

    This is the fallback tail of the full **provider-self-reported → models.dev →
    litellm catalog → local DB** cascade (the provider-self-report step runs
    upstream in ``enrich_capabilities``).

    Args:
        model_id: The raw model identifier (may include a ``vendor/`` prefix).
        provider_kind: The provider kind; accepted for call-site symmetry / a future
            source that disambiguates by backend (the current sources match on id).

    Returns:
        ``(context_window, source_name)`` where ``source_name`` is exactly
        ``"models.dev"``, ``"litellm"``, or ``"db"`` on a hit, or ``(None, "")`` on
        a total miss.
    """
    if not (model_id or "").strip():
        return None, ""

    window = lookup_models_dev(model_id)
    if window is not None:
        return window, SOURCE_MODELS_DEV

    window = lookup_litellm_context(model_id)
    if window is not None:
        return window, SOURCE_LITELLM

    window = db.lookup_context(model_id)
    if window is not None:
        return window, SOURCE_DB

    return None, ""


def resolve_output_limit(model_id: str, provider_kind: str) -> int | None:
    """Resolve max output tokens via models.dev, then LiteLLM, then the local DB."""
    if not (model_id or "").strip():
        return None
    output = lookup_models_dev_output(model_id)
    if output is not None:
        return output
    output = lookup_litellm_output(model_id)
    if output is not None:
        return output
    return db.lookup_output(model_id)
