"""iowarp-core 2.5's shared-memory cache in CLIO's deployment, metadata-only, opt-in.

iowarp-core 2.5 adds ``clio_cte_core_shm_cache_writethru`` (pool ``568.0``): one
shared-memory segment per node that client processes map. In ``md_only`` mode it holds
tag and blob *metadata* (name to tag id, blob sizes) and never blob bytes: a put or get
is still a task to the core (``shm_wtc_impl.h`` ``TryPut``/``TryGet`` miss when
``MdOnly()``), and only a size probe or a tag lookup can complete in the client.

Durability against CLIO's contract (:mod:`clio_agent.arc.clio_core_durability`): in
``md_only`` mode a put is acknowledged by the persistent core exactly as without the
cache (data written, WAL record appended), so a crash loses nothing the core had
acknowledged. The cache's data mode (``md_only: false``) acknowledges a put once the
bytes are in the shared segment, before any WAL record, reports a failed flush only
through ``SyncTag`` (not bound in Python), and loses unflushed ranges with the daemon;
CLIO does not offer it.

``arc.cte.shm_cache`` (``CLIO_ARC_CTE_SHM_CACHE``):

* ``off`` (default): CLIO's compose is the RAM arena, the core and the indexer.
* ``md_only``: the cache is composed between the indexer and the core
  (indexer ``next_pool_id`` -> ``568.0`` -> core ``512.0``), with ``entry_pool_id`` the
  indexer, so CLIO's client binding (``CLIO_CTE_POOL`` = the indexer) also maps the
  segment. Only the synchronous calls use it (``arc.clio_core.blob_calls=sync``,
  :mod:`clio_agent.arc.clio_core_sync_calls`).

Applied to CLIO's own seed on every start, both ways, like the other in-place seed
upgrades; a config without CLIO's indexer block is never touched. The daemon reads it
when it next starts.
"""

from __future__ import annotations

import functools
import logging
from pathlib import Path

from clio_agent import conf

__all__ = ["MD_ONLY", "OFF", "SHM_CACHE_POOL_ID", "apply_shm_cache", "mode", "shm_cache_block"]

logger = logging.getLogger(__name__)

OFF = "off"
MD_ONLY = "md_only"
_MODES = (OFF, MD_ONLY)
SHM_CACHE_POOL_ID = "568.0"
_INDEXER_MARKER = "  - mod_name: clio_cte_indexer\n"
_TO_CORE = '    next_pool_id: "512.0"\n'
_TO_CACHE = f'    next_pool_id: "{SHM_CACHE_POOL_ID}"\n'
#: Metadata only: 16Ki blobs and tags fit in 64 MB with its log (upstream's test uses
#: 128M for 16Ki blobs with a 16Ki-deep log; md_only needs no byte buffers).
_BLOCK = f"""\
  - mod_name: clio_cte_core_shm_cache_writethru
    pool_name: clio_cte_core_shm_cache_writethru
    pool_query: local
    pool_id: "{SHM_CACHE_POOL_ID}"
    next_pool_id: "512.0"
    entry_pool_id: "564.0"
    md_only: true
    capacity: "128M"
"""


@functools.cache
def mode() -> str:
    """The configured mode, read once per process."""
    raw = conf.resolve(
        "arc.cte.shm_cache", env="CLIO_ARC_CTE_SHM_CACHE", default=OFF, cast=conf.as_str
    )
    value = raw.strip().lower()
    if value not in _MODES:
        raise ValueError(f"arc.cte.shm_cache must be one of {_MODES}, got {raw!r}")
    return value


def shm_cache_block() -> str:
    """The ``compose`` entry CLIO adds for ``md_only``."""
    return _BLOCK


def _with_cache(text: str) -> str | None:
    if _BLOCK in text or text.count(_INDEXER_MARKER) != 1:
        return None
    start = text.index(_INDEXER_MARKER)
    indexer = text[start:]
    if indexer.count(_TO_CORE) != 1:
        return None
    return text[:start] + _BLOCK + indexer.replace(_TO_CORE, _TO_CACHE, 1)


def _without_cache(text: str) -> str | None:
    if text.count(_BLOCK) != 1:
        return None
    stripped = text.replace(_BLOCK, "", 1)
    start = stripped.index(_INDEXER_MARKER) if _INDEXER_MARKER in stripped else -1
    if start < 0 or stripped[start:].count(_TO_CACHE) != 1:
        return None
    return stripped[:start] + stripped[start:].replace(_TO_CACHE, _TO_CORE, 1)


def apply_shm_cache(path: Path) -> None:
    """Compose or remove the cache in CLIO's seed at ``path`` to match :func:`mode`."""
    text = path.read_text(encoding="utf-8")
    wanted = mode()
    upgraded = _with_cache(text) if wanted == MD_ONLY else _without_cache(text)
    if upgraded is None:
        return
    path.write_text(upgraded, encoding="utf-8")
    from clio_agent.arc.clio_core_growth import rekey_managed_capacity  # noqa: PLC0415 - cycle

    rekey_managed_capacity(path, text, upgraded)  # CLIO's own edit keeps growth
    logger.warning(
        "reason=clio_core_shm_cache_%s config=%s: the daemon uses it from its next start",
        "composed" if wanted == MD_ONLY else "removed",
        path,
    )
