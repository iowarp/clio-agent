"""Static last-resort context-window constants.

This is the final fallback in the context-source factory's strict order
(models.dev -> marketplace -> static). It ships a tiny, hand-maintained dict of
well-known model families whose context windows are stable and unlikely to need
per-deployment curation. Unlike :mod:`marketplace`, this table is *built in* and
never read from disk — it exists so a totally offline clio with no marketplace DB
and no cached models.dev copy can still size a request for a common model.

Keep this small: anything that needs frequent updates belongs in the marketplace
DB, not here.
"""

from __future__ import annotations

from clio_agent.providers.handshake.sources._normalize import iter_id_candidates

#: Last-resort context windows keyed by *normalized* model id (lowercased,
#: vendor prefix stripped). Deliberately tiny — common, slow-moving families only.
_STATIC_CONTEXT: dict[str, int] = {
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-3.5-turbo": 16_385,
    # Anthropic
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
    # Common open-weights baselines
    "llama-3-8b": 8_192,
    "llama-3-70b": 8_192,
    "mistral-7b": 32_768,
    "mixtral-8x7b": 32_768,
}


def lookup_static(model_id: str) -> int | None:
    """Return a built-in last-resort context window for ``model_id``, or None.

    Matching tolerates vendor prefixes and case the same way the other sources do:
    the id is normalized and each candidate form is probed against the static
    table.

    Args:
        model_id: The raw model identifier (may include a ``vendor/`` prefix).

    Returns:
        The context window in tokens, or ``None`` if the model is unknown here.
    """
    for candidate in iter_id_candidates(model_id):
        window = _STATIC_CONTEXT.get(candidate)
        if window is not None:
            return window
    return None
