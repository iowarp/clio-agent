"""clio-core's BM25 indexer in CLIO's deployment (owner module, clio-core#905).

``SemanticSearch`` lives in the optional ``clio_cte_indexer`` chimod, an interposer in
front of the CTE core: every put from a client bound to it passes through, is
forwarded to the core, and only enqueues its key; a background sweep tokenizes, and a
search drains the queue first so it sees its own writes. Without the chimod a search
returns no hits and does not raise.

CLIO composes it in the seeded config at :data:`INDEXER_POOL_ID` with the core as its
next pool, a persistent index log under the CTE data directory, and a scope that
tokenizes only plain-text search companions of memory documents and of the context
plane's segment search records (:data:`INDEX_TAG_RE`, :data:`INDEX_BLOB_RE`). Segment
bodies and ``_events`` chunks are rewritten constantly and stored base64-wrapped, so
they stay out of scope and cost one regex check per put. The client binds to the
indexer through ``CLIO_CTE_POOL``, the interposer redirect clio-core uses itself
(``test_indexer_restart.sh``); the binding is per process.

Qualified on iowarp-core 2.3.2 (memory ledger P0, MD1): restart and a killed daemon
restore the index from its log without a rescan, and the hop adds no measurable cost
to ARC's hot path.

A config without the chimod cannot search: :data:`CLIO_CORE_SEARCH_UNAVAILABLE` is
the typed reason every search caller and ``clio doctor`` report.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: Upstream's reserved indexer pool (``clio::cte::indexer::kIndexerPoolId``).
INDEXER_POOL_ID = "564.0"
#: The CTE core pool the indexer forwards to.
CORE_POOL_ID = "512.0"
#: Tags whose companions are tokenized: memory documents and context-plane segments,
#: in any namespace (``memory/memories``, ``segments``, a test's ``t-1a2b/segments``).
INDEX_TAG_RE = "(.+/)?(memories|segments)"
#: Only plain-text ``.text`` search companions are tokenized.
INDEX_BLOB_RE = r".*\.text"
#: The client-side pool redirect clio-core reads at attach.
POOL_ENV = "CLIO_CTE_POOL"

#: Typed reason: the resolved config composes no indexer, so nothing can be searched.
CLIO_CORE_SEARCH_UNAVAILABLE = "clio_core_search_unavailable"
UPSTREAM_INDEXER_ISSUE = "https://github.com/iowarp/clio-core/issues/905"
SEARCH_REMEDY = (
    "compose the clio_cte_indexer chimod in the clio-core config (CLIO's seeded config "
    "does; a hand-written one needs the block from docs/ARC_MEMORY_LAYER.md) and restart"
)

_BLOCK = """\
  - mod_name: clio_cte_indexer
    pool_name: clio_cte_indexer
    pool_query: local
    pool_id: "{pool}"
    restart: true
    next_pool_id: "{core}"
    tag_re: "{tag_re}"
    blob_re: "{blob_re}"
    index_log_path: "{index_log}"
"""


def indexer_block(index_log: str) -> str:
    """The ``compose`` entry CLIO seeds for the indexer, with its log at ``index_log``."""
    return _BLOCK.format(
        pool=INDEXER_POOL_ID,
        core=CORE_POOL_ID,
        tag_re=INDEX_TAG_RE,
        blob_re=INDEX_BLOB_RE.replace("\\", "\\\\"),
        index_log=index_log,
    )


def _indexer_module(config: Mapping[str, Any]) -> Mapping[str, Any] | None:
    for module in config.get("compose", []) or []:
        if isinstance(module, Mapping) and str(module.get("mod_name", "")) == "clio_cte_indexer":
            return module
    return None


def _load(path: Path) -> Mapping[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, Mapping) else {}


def indexer_pool(path: str | Path) -> str:
    """The indexer pool id ``path`` composes, or ``""`` when it composes none."""
    module = _indexer_module(_load(Path(path).expanduser())) if path else None
    return "" if module is None else str(module.get("pool_id", INDEXER_POOL_ID))


def indexer_chimod_present(
    *, env: Mapping[str, str], config_path: str | Path | None = None
) -> bool:
    """Whether the resolved clio-core config composes the indexer (read-only).

    Args:
        env: Environment driving the config-path resolution when ``config_path`` is
            not given (the doctor's view).
        config_path: The exact config file, when the caller already resolved it.

    Returns:
        ``True`` only when the file exists and composes ``clio_cte_indexer``.
    """
    from clio_agent.arc.clio_core_config import _resolve_config_path  # noqa: PLC0415

    path = Path(config_path).expanduser() if config_path else _resolve_config_path(env)
    return bool(indexer_pool(path))


def ensure_seeded_config_indexed(path: Path, index_log: Path) -> None:
    """Upgrade CLIO's own seeded config in place so it composes the indexer (once).

    The seeded config ends with its ``compose`` list, so the indexer entry is appended
    after the core it forwards to. The result is re-parsed; a file that does not take
    the entry the expected way is left as it was and reported, so ``clio doctor``
    shows search as unavailable with its remedy.
    """
    text = path.read_text(encoding="utf-8")
    if indexer_pool(path):
        return
    upgraded = text if text.endswith("\n") else text + "\n"
    upgraded += indexer_block(index_log.as_posix())
    config = yaml.safe_load(upgraded) or {}
    modules = [m.get("mod_name") for m in config.get("compose", []) or []]
    if modules[-1:] != ["clio_cte_indexer"] or "clio_cte_core" not in modules:
        logger.warning(
            "reason=%s config=%s problem=cannot add the indexer in place remedy=%s",
            CLIO_CORE_SEARCH_UNAVAILABLE,
            path,
            SEARCH_REMEDY,
        )
        return
    path.write_text(upgraded, encoding="utf-8")
    logger.warning(
        "clio-core config %s upgraded in place: it now composes the clio_cte_indexer "
        "chimod, so long-term memory and context recall can search (clio-core#905)",
        path,
    )


#: Marks a ``CLIO_CTE_POOL`` value CLIO set itself. Child processes inherit both, so a
#: child attaching with another config recomputes its binding instead of taking the
#: parent's pool for an operator's choice.
BOUND_BY_ENV = "CLIO_CTE_POOL_BOUND_BY"
_BOUND_BY = "clio-agent"


def bind_client_to_indexer(config_path: str, environ: MutableMapping[str, str]) -> str:
    """Point this process's CTE client at the indexer pool ``config_path`` composes.

    Must run before the native client attaches. An operator's explicit
    ``CLIO_CTE_POOL`` (for example a cache chain whose top forwards to the indexer)
    is kept as given; a value CLIO set (here or in a parent process) is recomputed
    for this config, so a client never routes to a pool its daemon does not compose
    (that hangs the first put: clio-core has no "pool not found" fast fail).

    Returns:
        The pool the client is bound to, or ``""`` when the config composes no
        indexer (search is then unavailable and reported as such).
    """
    current = environ.get(POOL_ENV, "").strip()
    if current and environ.get(BOUND_BY_ENV) != _BOUND_BY:
        return current  # the operator's explicit choice
    pool = indexer_pool(config_path)
    if pool:
        environ[POOL_ENV] = pool
        environ[BOUND_BY_ENV] = _BOUND_BY
        return pool
    environ.pop(POOL_ENV, None)
    environ.pop(BOUND_BY_ENV, None)
    logger.warning(
        "reason=%s config=%s problem=no clio_cte_indexer chimod composed remedy=%s",
        CLIO_CORE_SEARCH_UNAVAILABLE,
        config_path or "~/.clio/clio.yaml",
        SEARCH_REMEDY,
    )
    return ""


def client_pool(environ: Mapping[str, str] | None = None) -> str:
    """The pool this process's client is bound to (``""`` means the core directly)."""
    return (environ if environ is not None else os.environ).get(POOL_ENV, "").strip()


__all__ = [
    "BOUND_BY_ENV",
    "CLIO_CORE_SEARCH_UNAVAILABLE",
    "CORE_POOL_ID",
    "INDEXER_POOL_ID",
    "INDEX_BLOB_RE",
    "INDEX_TAG_RE",
    "POOL_ENV",
    "SEARCH_REMEDY",
    "UPSTREAM_INDEXER_ISSUE",
    "bind_client_to_indexer",
    "client_pool",
    "ensure_seeded_config_indexed",
    "indexer_block",
    "indexer_chimod_present",
    "indexer_pool",
]
