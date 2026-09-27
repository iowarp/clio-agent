"""Server-wide defaults a model server applies but does not report over its API.

Ollama applies a default context to every model it loads -- its
``OLLAMA_CONTEXT_LENGTH``, or a VRAM-based default when that is unset -- and
no Ollama endpoint reports it (only ``/api/ps`` shows the context of a model
already loaded). Before a model loads, that default is what will be in force,
unless the model's Modelfile sets ``num_ctx``.

The server's own startup log states it, and CLIO reads that log for the
servers it deploys (:mod:`clio_agent.gact.infrastructure.effective_parameters`).
This module is the seam between the two: the infrastructure layer registers a
lookup over its existing service records, and discovery asks it by address.
Nothing is stored here; an address CLIO did not deploy has no answer, and its
default stays unknown.
"""

from __future__ import annotations

from collections.abc import Callable

from clio_agent.providers.api_base import native_root
from clio_agent.providers.capabilities.records import Fact

ContextDefaultLookup = Callable[[str], Fact[int] | None]

#: One lookup per owner: a CLIO app registering again (a restart, a test app)
#: replaces its earlier lookup instead of accumulating stale ones.
_lookups: dict[str, ContextDefaultLookup] = {}


def register_context_default_lookup(owner: str, lookup: ContextDefaultLookup) -> None:
    """Set ``owner``'s lookup ``native root URL -> the server's configured default context``."""

    _lookups[owner] = lookup


def unregister_context_default_lookup(owner: str) -> None:
    """Remove ``owner``'s lookup."""

    _lookups.pop(owner, None)


def context_default(api_base: str) -> Fact[int] | None:
    """The default context the server at ``api_base`` applies to models it loads, if known."""

    root = native_root(api_base).rstrip("/")
    for lookup in list(_lookups.values()):
        fact = lookup(root)
        if fact is not None and fact.known:
            return fact
    return None


__all__ = [
    "ContextDefaultLookup",
    "context_default",
    "register_context_default_lookup",
    "unregister_context_default_lookup",
]
