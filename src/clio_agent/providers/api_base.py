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

:func:`normalize` is the second piece of shared surgery: canonicalizing an
``api_base`` before it is used inside an identity key (model-capabilities
brief Part 3 — endpoint keys are ``(provider_id, api_base)``, deployment keys
add ``model_id``). Two cosmetically different strings for the same endpoint
(a trailing slash, a default port spelled out, a different scheme case) must
never produce two different cache/snapshot entries for what is really one
server.
"""

from __future__ import annotations

from urllib.parse import SplitResult, urlsplit, urlunsplit

#: Ports LiteLLM/requests would use implicitly for these schemes -- an
#: explicit port equal to the default is noise, not a distinct endpoint.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def native_root(api_base: str) -> str:
    """Return ``api_base`` with a trailing ``/v1`` (and trailing slashes) stripped.

    ``http://host:1234/v1`` -> ``http://host:1234``; ``http://host:1234/v1/`` ->
    ``http://host:1234``; a base with no ``/v1`` suffix is returned unchanged
    (after stripping trailing slashes).
    """
    base = (api_base or "").rstrip("/")
    return base[: -len("/v1")] if base.endswith("/v1") else base


def normalize(api_base: str) -> str:
    """Return the canonical form of ``api_base`` for use inside an identity key.

    Canonicalizes exactly the fields the model-capabilities brief names —
    scheme, host, port, and a path with no trailing slash — so equivalent
    spellings collapse to one key:

    * ``HTTP://Host:80/v1/`` -> ``http://host/v1`` (scheme + host lowercased,
      the http-default port 80 dropped, the trailing slash stripped).
    * ``http://host:11434/v1`` is unchanged: a non-default port and a bare
      ``/v1`` path (no trailing slash) both stay exactly as given.

    A value with no ``scheme://host`` shape (a bare host, or an SDK identity
    marker like ``codex://sdk``) has nothing safe to canonicalize beyond the
    trailing slash, so only that is stripped. Callers that need a key rather
    than a display string should always route ``api_base`` through this
    before pairing it with a ``provider_id``.
    """
    base = (api_base or "").strip()
    if not base:
        return base
    parts: SplitResult = urlsplit(base)
    if not parts.scheme or not parts.netloc:
        return base.rstrip("/") or base
    scheme = parts.scheme.lower()
    hostname = parts.hostname or ""
    port = parts.port
    default_port = _DEFAULT_PORTS.get(scheme)
    netloc = hostname if port is None or port == default_port else f"{hostname}:{port}"
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parts.query, parts.fragment))


__all__ = ["native_root", "normalize"]
