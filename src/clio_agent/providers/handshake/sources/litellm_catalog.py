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


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def lookup_litellm(model_id: str, *, allow_fetch: bool = True) -> tuple[int | None, int | None]:
    """Return ``(context_window, output_limit)`` from LiteLLM, or ``(None, None)``.

    The map is loaded ONCE per call, then every id variant is probed against it
    (the first variant carrying either limit wins).

    Args:
        model_id: The raw model identifier (with or without a provider prefix).
        allow_fetch: When ``False``, never touch the network -- disk cache or
            the bundled wheel snapshot only (the offline-safe test path).
    """
    if not (model_id or "").strip():
        return None, None
    cost_map = _cost_map(allow_fetch=allow_fetch)
    for candidate in _id_variants(model_id):
        info = cost_map.get(candidate)
        if not isinstance(info, dict):
            continue
        ctx = _positive_int(info.get("max_input_tokens") or info.get("max_tokens"))
        out = _positive_int(info.get("max_output_tokens"))
        if ctx or out:
            return ctx, out
    return None, None


def lookup_litellm_info(
    model_id: str, *, allow_fetch: bool = True
) -> tuple[str, dict[str, Any]] | None:
    """Return ``(matched_key, info)`` for the first id variant LiteLLM lists, or None.

    One map load per call; the matched key is returned so a caller can name
    exactly which LiteLLM row its facts came from.
    """
    if not (model_id or "").strip():
        return None
    cost_map = _cost_map(allow_fetch=allow_fetch)
    for candidate in _id_variants(model_id):
        info = cost_map.get(candidate)
        if isinstance(info, dict):
            return candidate, dict(info)
    return None


#: LiteLLM ``mode`` -> CLIO model type. LiteLLM's own spellings ARE the model
#: type vocabulary (see ``records.ModelType``); ``responses`` is a chat model
#: served only through the Responses API, which LiteLLM bridges for chat. Any
#: other mode (``completion``, ``moderation``, ...) is not mapped: it stays
#: unknown rather than being forced into the nearest type.
_MODE_TO_MODEL_TYPE: dict[str, str] = {
    "chat": "chat",
    "responses": "chat",
    "embedding": "embedding",
    "rerank": "rerank",
    "audio_transcription": "audio_transcription",
    "audio_speech": "audio_speech",
    "image_generation": "image_generation",
}


def model_type_from_info(info: dict[str, Any]) -> str | None:
    """The CLIO model type a LiteLLM row's ``mode`` names, or None."""
    mode = info.get("mode")
    return _MODE_TO_MODEL_TYPE.get(mode) if isinstance(mode, str) else None


def modalities_from_info(info: dict[str, Any]) -> frozenset[str] | None:
    """Input modalities from a LiteLLM row's ``supports_*`` flags, or None.

    LiteLLM omits a flag far more often than it sets one to ``False``, so the set
    is only known when the row states ``supports_vision`` explicitly -- an entry
    with no vision flag at all is no evidence of a text-only model. When it is
    stated, ``supports_audio_input`` and ``supports_pdf_input`` add their
    modality only when explicitly ``True``.
    """
    vision = info.get("supports_vision")
    if not isinstance(vision, bool):
        return None
    modalities = {"text"}
    if vision:
        modalities.add("image")
    if info.get("supports_audio_input") is True:
        modalities.add("audio")
    if info.get("supports_pdf_input") is True:
        modalities.add("pdf")
    return frozenset(modalities)


def lookup_litellm_context(model_id: str, *, allow_fetch: bool = True) -> int | None:
    """Context window for ``model_id`` from LiteLLM, or None."""
    return lookup_litellm(model_id, allow_fetch=allow_fetch)[0]


def lookup_litellm_output(model_id: str, *, allow_fetch: bool = True) -> int | None:
    """Max output tokens for ``model_id`` from LiteLLM, or None."""
    return lookup_litellm(model_id, allow_fetch=allow_fetch)[1]
