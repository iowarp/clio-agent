"""Shared ``api_base`` URL surgery for provider dialects.

Some provider dialects speak their NATIVE API at the ``api_base`` host root
rather than under an OpenAI-compatible ``/v1`` path: Ollama's native
``/api/*`` surface, LM Studio's native ``/api/v0``, and LiteLLM's
``ollama_chat`` provider (which appends its own ``/api/chat`` to whatever
``api_base`` it is given). Stripping a trailing ``/v1`` is the one piece of
URL surgery every one of those call sites needs, so it lives here once
instead of as near-identical copies in each handshake plus the LM factory
(iowarp/clio-agent#1413 — the previous duplicates let the factory's
``ollama_chat`` connection keep an unstripped ``/v1``, doubling into
``/v1/api/chat`` and 404ing).
"""

from __future__ import annotations


def native_root(api_base: str) -> str:
    """Return ``api_base`` with a trailing ``/v1`` (and trailing slashes) stripped.

    ``http://host:1234/v1`` -> ``http://host:1234``; ``http://host:1234/v1/`` ->
    ``http://host:1234``; a base with no ``/v1`` suffix is returned unchanged
    (after stripping trailing slashes).
    """
    base = (api_base or "").rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


__all__ = ["native_root"]
