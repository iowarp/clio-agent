"""The memory store: a ``ClioCoreStore`` in namespace ``memory`` with its own kinds.

Records live under tags ``memory/<kind>`` and never mix with ARC's context plane. The
kinds are not ARC kinds; the store is built with :data:`MEMORY_KINDS` so its
``clear`` and tag prewarm cover exactly them. ``memories`` companions are in the BM25
indexer's scope (:data:`clio_agent.arc.clio_core_indexer.INDEX_TAG_RE`); the other
kinds carry no companion, so a counter update never re-tokenizes anything.
"""

from __future__ import annotations

from clio_agent import conf
from clio_agent.arc.store_protocol import ARCStore

MEMORY_KINDS: tuple[str, ...] = ("memories", "memory_versions", "memory_proposals", "memory_usage")
DOCUMENTS = "memories"
VERSIONS = "memory_versions"
PROPOSALS = "memory_proposals"
USAGE = "memory_usage"


def memory_namespace() -> str:
    """``memory``, nested under ARC's own namespace when one is configured (tests)."""
    base = conf.resolve("arc.namespace", env="CLIO_ARC_NAMESPACE", default="", cast=conf.as_str)
    base = base.strip().strip("/")
    return f"{base}/memory" if base else "memory"


def open_memory_store() -> ARCStore:
    """Attach the memory store (the process shares one clio-core client)."""
    from clio_agent.arc import storage  # noqa: PLC0415 - patched by the test suite

    return storage.make_arc_store(namespace=memory_namespace(), kinds=MEMORY_KINDS)


__all__ = [
    "DOCUMENTS",
    "MEMORY_KINDS",
    "PROPOSALS",
    "USAGE",
    "VERSIONS",
    "memory_namespace",
    "open_memory_store",
]
