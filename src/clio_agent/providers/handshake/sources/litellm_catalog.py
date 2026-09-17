"""LiteLLM model-info source — clio's own provider runtime is LiteLLM (via DSPy).

LiteLLM ships a curated catalog (``model_prices_and_context_window.json``) exposed
through ``litellm.get_model_info(model)``, which returns ``max_input_tokens`` (the
context window) and ``max_output_tokens`` for thousands of known models. This is
the natural metadata source for the **cloud** providers (OpenAI, Anthropic,
OpenRouter, ...) whose ``/models`` APIs report no context at all. CLIO uses the
release-pinned snapshot as a deterministic metadata fallback.

Offline-safe: lookups read the catalog bundled with the pinned ``litellm`` wheel.
They intentionally do not call ``litellm.get_model_info`` because importing LiteLLM
fetches a mutable catalog from its upstream ``main`` branch by default. The bundled
snapshot keeps release behavior reproducible. Catalog keys are often
provider-prefixed, so we probe a few id variants before giving up.
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from clio_agent.providers.handshake.sources._normalize import iter_id_candidates

#: Provider prefixes LiteLLM uses to key cloud models; tried in addition to the
#: bare id so e.g. ``claude-sonnet-4-...`` resolves via ``anthropic/claude-...``.
_PREFIXES = ("anthropic/", "openai/", "openrouter/", "mistral/", "gemini/")


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


@lru_cache(maxsize=1)
def _bundled_model_cost_map() -> dict[str, Any]:
    """Load the immutable model catalog shipped in the pinned LiteLLM wheel."""
    try:
        text = (
            resources.files("litellm")
            .joinpath("model_prices_and_context_window_backup.json")
            .read_text(encoding="utf-8")
        )
        payload = json.loads(text)
    except (
        FileNotFoundError,
        ModuleNotFoundError,
        OSError,
        TypeError,
        json.JSONDecodeError,
    ):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {key: value for key, value in payload.items() if isinstance(key, str)}


def _get_model_info(candidate: str) -> dict[str, Any] | None:
    info = _bundled_model_cost_map().get(candidate)
    return dict(info) if isinstance(info, dict) else None


def lookup_litellm(model_id: str) -> tuple[int | None, int | None]:
    """Return ``(context_window, output_limit)`` from LiteLLM, or ``(None, None)``."""
    if not (model_id or "").strip():
        return None, None
    for candidate in _id_variants(model_id):
        info = _get_model_info(candidate)
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


def lookup_litellm_context(model_id: str) -> int | None:
    """Context window for ``model_id`` from LiteLLM, or None."""
    return lookup_litellm(model_id)[0]


def lookup_litellm_output(model_id: str) -> int | None:
    """Max output tokens for ``model_id`` from LiteLLM, or None."""
    return lookup_litellm(model_id)[1]
