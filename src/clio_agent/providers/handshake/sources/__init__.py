"""Context-source factory: resolve a model's limits, modalities and task from the cascade.

Limits use the full cascade below. Input modalities and the model task come
from the same two public catalogs (models.dev ``modalities``, LiteLLM
``supports_*`` flags and ``mode``) through :func:`resolve_input_modalities` and
:func:`resolve_task`; a miss there is UNKNOWN, never a text-only default.

The full cascade is **provider-self-reported → models.dev → litellm catalog →
local DB**. A provider's own metadata is authoritative; when it doesn't
self-report a limit, the handshake's ``enrich_capabilities`` step falls back to
this factory, which tries the remaining sources in a **strict, fixed order** and
returns the first hit with an exact provenance string:

1. **models.dev** (``"models.dev"``) — the public catalog, fetched + cached with a
   TTL, offline-safe. Broadest coverage.
2. **litellm** (``"litellm"``) — the LiteLLM community model-cost map, fetched +
   cached with a TTL the same way (see
   :mod:`clio_agent.providers.handshake.sources.litellm_catalog`), for models
   models.dev doesn't list.
3. **db** (``"db"``) — the local model-limits database (:mod:`...sources.db`): a
   repo-shipped, lab-shareable JSON that is also **written back on discovery**, so
   models no public catalog lists yet are still known offline next time.

On a total miss the factory returns ``(None, "")`` and the caller keeps the
model's limit unset. Provenance strings are exactly ``models.dev`` | ``litellm``
| ``db`` and feed the ``Fact.source`` on a
:class:`~clio_agent.providers.capabilities.records.ModelCapabilities`. Nothing
here fetches at import time.
"""

from __future__ import annotations

from clio_agent.providers.handshake.sources import db
from clio_agent.providers.handshake.sources.litellm_catalog import (
    lookup_litellm_context,
    lookup_litellm_info,
    lookup_litellm_output,
    modalities_from_info,
    task_from_info,
)
from clio_agent.providers.handshake.sources.models_dev import (
    lookup_models_dev,
    lookup_models_dev_entry,
    lookup_models_dev_output,
    modalities_from_entry,
    task_from_output,
)

__all__ = [
    "db",
    "lookup_litellm_context",
    "lookup_litellm_output",
    "lookup_models_dev",
    "lookup_models_dev_output",
    "lookup_native_context",
    "resolve_context",
    "resolve_input_modalities",
    "resolve_task",
    "resolve_output_limit",
]

#: Provenance string for the models.dev source.
SOURCE_MODELS_DEV = "models.dev"
#: Provenance string for the LiteLLM catalog source.
SOURCE_LITELLM = "litellm"
#: Provenance string for the local DB source.
SOURCE_DB = "db"


def lookup_native_context(model_id: str) -> int | None:
    """Return the offline catalog's published max context for ``model_id``, or None.

    Walks the same offline-only ladder as
    :func:`clio_agent.gact.runtime.context_tokens._resolve_expert_context_window`:
    the LiteLLM catalog first (disk cache / bundled snapshot only --
    ``allow_fetch=False``, so this never fetches), then the bundled
    ``model_limits.json`` DB. No network call is made. Returns None when neither
    source has an entry, so the caller can leave ``native_context_window`` unset
    rather than guessing.

    This is intentionally distinct from :func:`resolve_context`, which includes the
    network-based models.dev source and is used for the *served* window; here we only
    want the authoritative published max from a fully offline catalog so the
    ``context_window_below_native`` warning can fire without any I/O at handshake time.
    """
    if not (model_id or "").strip():
        return None
    ctx = lookup_litellm_context(model_id, allow_fetch=False)
    if ctx is not None:
        return ctx
    return db.lookup_context(model_id)


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


def resolve_input_modalities(model_id: str) -> tuple[frozenset[str] | None, str, str]:
    """Resolve a model's INPUT modalities via models.dev, then LiteLLM.

    models.dev states ``modalities.input`` outright; LiteLLM only through its
    ``supports_vision``/``supports_audio_input``/``supports_pdf_input`` flags,
    and only counts when ``supports_vision`` is stated at all (see
    :func:`~.litellm_catalog.modalities_from_info`). The local DB records limits
    only, so it has no tier here.

    Returns:
        ``(modalities, source_name, detail)`` -- ``source_name`` is exactly
        ``"models.dev"`` or ``"litellm"`` on a hit; ``(None, "", "")`` when no
        catalog states the model's modalities. A miss is UNKNOWN, never "text".
    """
    if not (model_id or "").strip():
        return None, "", ""
    entry = lookup_models_dev_entry(model_id)
    modalities, _output = modalities_from_entry(entry)
    if modalities:
        return modalities, SOURCE_MODELS_DEV, f"models.dev {_entry_id(entry)} modalities.input"
    matched = lookup_litellm_info(model_id)
    if matched is not None:
        key, info = matched
        modalities = modalities_from_info(info)
        if modalities is not None:
            return modalities, SOURCE_LITELLM, f"litellm {key} supports_vision/audio/pdf flags"
    return None, "", ""


def resolve_task(model_id: str) -> tuple[str | None, str, str]:
    """Resolve a model's task via LiteLLM ``mode``, then models.dev output modalities.

    LiteLLM's ``mode`` names the task directly and is tried first. models.dev has
    no type field; its output modalities only decide a type when they lack text
    (:func:`~.models_dev.task_from_output`).

    Returns:
        ``(task, source_name, detail)``, or ``(None, "", "")`` on a miss.
    """
    if not (model_id or "").strip():
        return None, "", ""
    matched = lookup_litellm_info(model_id)
    if matched is not None:
        key, info = matched
        task = task_from_info(info)
        if task is not None:
            return task, SOURCE_LITELLM, f"litellm {key} mode={info.get('mode')!r}"
    entry = lookup_models_dev_entry(model_id)
    _input, output = modalities_from_entry(entry)
    task = task_from_output(output)
    if task is not None:
        return task, SOURCE_MODELS_DEV, f"models.dev {_entry_id(entry)} modalities.output"
    return None, "", ""


def _entry_id(entry: object) -> str:
    return str(entry.get("id") or "") if isinstance(entry, dict) else ""
