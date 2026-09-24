"""LiteLLM community model-cost map — clio's own provider runtime is LiteLLM (via DSPy).

LiteLLM's community-maintained ``model_prices_and_context_window.json`` reports
``max_input_tokens`` (the context window) and ``max_output_tokens`` for thousands
of known models. This is the natural metadata source for the **cloud** providers
(OpenAI, Anthropic, OpenRouter, ...) whose ``/models`` APIs report no context at
all.

This module fetches the SAME map LiteLLM itself would (its own
``litellm.model_cost_map_url``), through the generic
:mod:`clio_agent.providers.fetched_catalog` mechanism: a disk cache with a TTL
and ETag under ``paths.user_cache_dir()/catalogs/litellm-model-cost-map.json``,
atomic writes, and a last-good copy that a failed fetch or a failed validation
never clears. The map bundled with the installed ``litellm`` wheel
(``model_prices_and_context_window_backup.json``) is used ONLY as the cold-start
fallback when there is no disk cache yet and no network — never as a ceiling on
freshness. Reproducibility no longer comes from freezing to the pinned wheel's
snapshot; it comes from the recorded ETag/version in every
:class:`~clio_agent.providers.fetched_catalog.CatalogResult`, which any lookup
can be re-run against.

Catalog keys are often provider-prefixed, so we probe a few id variants before
giving up.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from clio_agent.providers.fetched_catalog import FetchedCatalog, FetchedCatalogUnavailable
from clio_agent.providers.handshake.sources._normalize import iter_id_candidates

#: Provider prefixes LiteLLM uses to key cloud models; tried in addition to the
#: bare id so e.g. ``claude-sonnet-4-...`` resolves via ``anthropic/claude-...``.
_PREFIXES = ("anthropic/", "openai/", "openrouter/", "mistral/", "gemini/")

#: Fallback if the installed litellm no longer exposes ``model_cost_map_url``
#: (mirrors litellm's own default; see ``litellm/__init__.py``).
_DEFAULT_COST_MAP_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
)

#: Community map refresh cadence — pricing/context entries change infrequently.
DEFAULT_TTL_S = 24 * 60 * 60.0

_FETCH_TIMEOUT_S = 8.0

#: The map is a few MB today; cap generously against a corrupted/hostile upstream.
_MAX_BYTES = 32 * 1024 * 1024


def _cost_map_url() -> str:
    """The URL LiteLLM itself fetches its community cost map from."""
    try:
        import litellm  # noqa: PLC0415
    except Exception:  # noqa: BLE001 - litellm is a hard dep but never break on lookup
        return _DEFAULT_COST_MAP_URL
    return str(getattr(litellm, "model_cost_map_url", "") or _DEFAULT_COST_MAP_URL)


def _parse_cost_map(payload: bytes) -> dict[str, Any]:
    """Parse+validate the LiteLLM cost-map JSON into ``{model_key: info}``.

    Raises:
        ValueError: The payload is not valid JSON, or not a JSON object.
    """
    data = json.loads(payload.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("litellm model cost map is not a JSON object")
    return {key: value for key, value in data.items() if isinstance(key, str)}


def _bundled_model_cost_map() -> dict[str, Any]:
    """Cold-start-only fallback: the map bundled with the pinned litellm wheel."""
    text = (
        resources.files("litellm")
        .joinpath("model_prices_and_context_window_backup.json")
        .read_text(encoding="utf-8")
    )
    return _parse_cost_map(text.encode("utf-8"))


@lru_cache(maxsize=1)
def _catalog() -> FetchedCatalog[dict[str, Any]]:
    """The (process-singleton, lazily built) :class:`FetchedCatalog` for the live cost map.

    Built lazily -- not at module import -- so merely importing this module (or
    anything that imports it, e.g. ``handshake.sources``) never forces
    ``import litellm`` or touches the network; only an actual lookup does.
    Memoised so every caller shares one instance (and therefore one refresh
    lock and one view of the disk cache).
    """
    return FetchedCatalog(
        "litellm-model-cost-map",
        _cost_map_url(),
        parse=_parse_cost_map,
        ttl_s=DEFAULT_TTL_S,
        max_bytes=_MAX_BYTES,
        timeout_s=_FETCH_TIMEOUT_S,
        bundled=_bundled_model_cost_map,
    )


def _id_variants(model_id: str) -> list[str]:
    variants: list[str] = []
    for candidate in iter_id_candidates(model_id):
        if candidate not in variants:
            variants.append(candidate)
    for candidate in list(variants):
        for prefix in _PREFIXES:
            prefixed = f"{prefix}{candidate}"
            if prefixed not in variants:
                variants.append(prefixed)
    return variants


def _cost_map(*, allow_fetch: bool = True) -> dict[str, Any]:
    """Return the current cost map, or ``{}`` on a total miss (never raises)."""
    try:
        return _catalog().get(allow_fetch=allow_fetch).data
    except FetchedCatalogUnavailable:
        return {}


def _get_model_info(candidate: str, *, allow_fetch: bool = True) -> dict[str, Any] | None:
    info = _cost_map(allow_fetch=allow_fetch).get(candidate)
    return dict(info) if isinstance(info, dict) else None


def lookup_litellm(model_id: str, *, allow_fetch: bool = True) -> tuple[int | None, int | None]:
    """Return ``(context_window, output_limit)`` from LiteLLM, or ``(None, None)``.

    Args:
        model_id: The raw model identifier (with or without a provider prefix).
        allow_fetch: When ``False``, never touch the network -- disk cache or
            the bundled wheel snapshot only (the offline-safe test path).
    """
    if not (model_id or "").strip():
        return None, None
    for candidate in _id_variants(model_id):
        info = _get_model_info(candidate, allow_fetch=allow_fetch)
        if not info:
            continue
        raw_ctx = info.get("max_input_tokens") or info.get("max_tokens")
        raw_out = info.get("max_output_tokens")
        ctx = (
            raw_ctx
            if isinstance(raw_ctx, int) and not isinstance(raw_ctx, bool) and raw_ctx > 0
            else None
        )
        out = (
            raw_out
            if isinstance(raw_out, int) and not isinstance(raw_out, bool) and raw_out > 0
            else None
        )
        if ctx or out:
            return ctx, out
    return None, None


def lookup_litellm_context(model_id: str, *, allow_fetch: bool = True) -> int | None:
    """Context window for ``model_id`` from LiteLLM, or None."""
    return lookup_litellm(model_id, allow_fetch=allow_fetch)[0]


def lookup_litellm_output(model_id: str, *, allow_fetch: bool = True) -> int | None:
    """Max output tokens for ``model_id`` from LiteLLM, or None."""
    return lookup_litellm(model_id, allow_fetch=allow_fetch)[1]
