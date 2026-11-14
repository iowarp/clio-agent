"""Config keys clio no longer supports, and the check that refuses a leftover one.

A removed key that is still set in a config file or the environment is a typed
``config_key_removed`` error naming what replaced it and every place it is set --
never ignored or remapped (fail over fallback). Kept a leaf module: everything it
needs is imported per call.
"""

from __future__ import annotations

#: Config keys clio no longer supports: (config-file key, env var, what was removed).
#: A key here that is still set is a typed error, never ignored or remapped. The fixed
#: MCP deadlines live beside their replacement
#: (:data:`clio_agent.tools.mcp_server_progress.REMOVED_DEADLINE_KEYS`).
REMOVED_CONFIG_KEYS: tuple[tuple[str, str, str], ...] = (
    (
        "lm.codex_variant",
        "CLIO_CODEX_VARIANT",
        "The Codex SDK path was removed (Codex now always connects directly)",
    ),
    (
        "artifacts.table_query_timeout_s",
        "CLIO_ARTIFACTS_TABLE_QUERY_TIMEOUT_S",
        "The fixed table-query timeout was replaced by a progress-based wait "
        "(artifacts.table_query_no_progress_s, artifacts.table_query_max_wait_s)",
    ),
    (
        "search.backend",
        "CLIO_SEARCH_BACKEND",
        "CLIO's private SearXNG was removed: search uses hosted sources "
        "(search.web.order, search.service.url; Settings → Web search)",
    ),
    (
        "search.clio_web_search.url",
        "CLIO_SEARCH_CLIO_WEB_SEARCH_URL",
        "CLIO's private SearXNG was removed: search uses hosted sources "
        "(search.web.order, search.service.url; Settings → Web search)",
    ),
    (
        "search.local_searxng.auto_install",
        "CLIO_SEARCH_AUTO_INSTALL",
        "CLIO's private SearXNG was removed: search uses hosted sources "
        "(search.web.order, search.service.url; Settings → Web search)",
    ),
    (
        "search.searxng.engines",
        "CLIO_SEARCH_SEARXNG_ENGINES",
        "CLIO's private SearXNG was removed: search uses hosted sources "
        "(search.web.order, search.service.url; Settings → Web search)",
    ),
    (
        "search.searxng.opt_in_engines",
        "CLIO_SEARCH_SEARXNG_OPT_IN_ENGINES",
        "CLIO's private SearXNG was removed: search uses hosted sources "
        "(search.web.order, search.service.url; Settings → Web search)",
    ),
    (
        "search.searxng.port",
        "CLIO_SEARCH_SEARXNG_PORT",
        "CLIO's private SearXNG was removed: search uses hosted sources "
        "(search.web.order, search.service.url; Settings → Web search)",
    ),
    (
        "search.searxng.safe_search",
        "CLIO_SEARCH_SEARXNG_SAFE_SEARCH",
        "CLIO's private SearXNG was removed: search uses hosted sources "
        "(search.web.order, search.service.url; Settings → Web search)",
    ),
    (
        "search.searxng.request_timeout_s",
        "CLIO_SEARCH_SEARXNG_REQUEST_TIMEOUT_S",
        "CLIO's private SearXNG was removed: search uses hosted sources "
        "(search.web.order, search.service.url; Settings → Web search)",
    ),
    (
        "search.searxng.max_results",
        "CLIO_SEARCH_SEARXNG_MAX_RESULTS",
        "CLIO's private SearXNG was removed: search uses hosted sources "
        "(search.web.order, search.service.url; Settings → Web search)",
    ),
    (
        "search.searxng.language",
        "CLIO_SEARCH_SEARXNG_LANGUAGE",
        "CLIO's private SearXNG was removed: search uses hosted sources "
        "(search.web.order, search.service.url; Settings → Web search)",
    ),
)


def reject_removed_config_keys() -> None:
    """Raise when a removed config key is still set in a config file or the environment.

    Raises:
        RemovedConfigKeyError: naming the removed feature, the key, and every
            place it is set (each config file path / the environment variable).
    """
    from clio_agent import conf  # noqa: PLC0415 - leaf module; lazy per-call
    from clio_agent.errors import RemovedConfigKeyError  # noqa: PLC0415
    from clio_agent.tools.mcp_server_progress import REMOVED_DEADLINE_KEYS  # noqa: PLC0415

    for key, env, removed in (*REMOVED_CONFIG_KEYS, *REMOVED_DEADLINE_KEYS):
        locations = conf.store().where_set(key, env=env)
        if locations:
            raise RemovedConfigKeyError(
                f"{removed}; delete {key} from {' and '.join(locations)}.",
                key=key,
                locations=locations,
            )
