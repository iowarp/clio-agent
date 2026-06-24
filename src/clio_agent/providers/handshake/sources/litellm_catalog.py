"""LiteLLM model-info source — clio's own provider runtime is LiteLLM (via DSPy).

LiteLLM ships a curated catalog (``model_prices_and_context_window.json``) exposed
through ``litellm.get_model_info(model)``, which returns ``max_input_tokens`` (the
context window) and ``max_output_tokens`` for thousands of known models. This is
the natural metadata source for the **cloud** providers (OpenAI, Anthropic,
OpenRouter, ...) whose ``/models`` APIs report no context at all — and it is the
same catalog that bounds clio's actual requests at runtime.

Offline-safe: the catalog is bundled with the ``litellm`` package, so lookups make
no network call. ``get_model_info`` is strict about ids (it wants the exact mapped
key, often provider-prefixed), so we probe a few id variants before giving up.
"""

from __future__ import annotations

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


def _get_model_info(candidate: str) -> dict[str, Any] | None:
    try:
        import litellm  # noqa: PLC0415
    except Exception:
        return None
    try:
        info = litellm.get_model_info(candidate)
    except Exception:
        # get_model_info raises for unmapped ids — a clean miss, try the next.
        return None
    # get_model_info returns a ModelInfo TypedDict; coerce to a plain dict for the
    # declared dict[str, Any] | None return (a clean miss is None).
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
