"""The ``search.*`` configuration: which backend serves web search, and its engine policy.

``search.backend`` selects the backend:

* ``local_searxng`` (default) -- CLIO's own SearXNG, a managed native service on
  this computer's loopback, installed on first use without containers;
* ``clio_web_search`` -- a CLIO Web Search gateway at ``search.clio_web_search.url``
  (a CLIO-managed deployment, or one on another machine so search traffic leaves
  from that machine's address instead of this one's);
* ``none`` -- no web search; the ``web_search`` tool answers with a typed
  ``search_not_configured`` error that names the fix.

Engine policy for the private SearXNG: a short list of reputable general and
scholarly engines is enabled; engines operated under Chinese jurisdiction
(:data:`OPT_IN_ONLY_PREFIXES`) are never enabled by ``search.searxng.engines``
alone and require an explicit ``search.searxng.opt_in_engines`` entry. Engines
that need an API key read it from CLIO's credential store
(:func:`engine_credential_ref`), never from a settings file or an argument.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

from clio_agent import conf

SearchBackendName = Literal["local_searxng", "clio_web_search", "none"]
BACKENDS: tuple[SearchBackendName, ...] = ("local_searxng", "clio_web_search", "none")

#: Enabled by default: reputable general engines, then scholarly ones. Every name
#: exists in SearXNG's settings.yml at the pinned commit (see searxng_service).
#: A literal list so the generated configuration reference shows it.
DEFAULT_ENGINE_LIST: list[str] = [
    "duckduckgo",
    "brave",
    "mojeek",
    "qwant",
    "startpage",
    "wikipedia",
    "arxiv",
    "crossref",
    "semantic scholar",
    "pubmed",
]
DEFAULT_ENGINES: tuple[str, ...] = tuple(DEFAULT_ENGINE_LIST)

#: Engines (by name prefix) that are enabled only by an explicit opt-in.
OPT_IN_ONLY_PREFIXES: tuple[str, ...] = (
    "baidu",
    "sogou",
    "360search",
    "chinaso",
    "quark",
    "bilibili",
    "iqiyi",
    "acfun",
)

#: Engines that need an API key, mapped to the engine setting that carries it.
API_KEY_ENGINES: dict[str, str] = {
    "braveapi": "api_key",
    "core.ac.uk": "api_key",
    "springer nature": "api_key",
    "marginalia": "api_key",
}


class SearchConfigurationError(ValueError):
    """A ``search.*`` value CLIO cannot use; the message names the key and the fix."""

    code = "search_configuration_invalid"


def engine_credential_ref(engine: str) -> str:
    """The credential-store id of one engine's API key."""

    return f"search-engine:{engine}"


def is_opt_in_only(engine: str) -> bool:
    """Whether ``engine`` may run only when explicitly opted in."""

    return engine.strip().casefold().startswith(OPT_IN_ONLY_PREFIXES)


def effective_engines(
    requested: list[str] | tuple[str, ...], opt_in: list[str] | tuple[str, ...] = ()
) -> tuple[list[str], list[str]]:
    """Apply the engine policy: ``(enabled, dropped)``, both in request order.

    An opt-in-only engine is enabled only when it is ALSO named in ``opt_in``;
    an engine named only in ``opt_in`` is enabled too (the opt-in is explicit).
    """

    allowed = {name.strip().casefold() for name in opt_in if name.strip()}
    enabled: list[str] = []
    dropped: list[str] = []
    for name in [*requested, *opt_in]:
        engine = name.strip()
        if not engine or engine in enabled or engine in dropped:
            continue
        if is_opt_in_only(engine) and engine.casefold() not in allowed:
            dropped.append(engine)
        else:
            enabled.append(engine)
    return enabled, dropped


@dataclass(frozen=True)
class SearchSettings:
    """The resolved ``search.*`` configuration."""

    backend: SearchBackendName = "local_searxng"
    clio_web_search_url: str = ""
    auto_install: bool = True
    port: int = 18890
    engines: tuple[str, ...] = DEFAULT_ENGINES
    dropped_engines: tuple[str, ...] = ()
    safe_search: int = 1
    request_timeout_s: float = 10.0
    max_results: int = 10
    language: str = "en"
    notes: tuple[str, ...] = field(default_factory=tuple)


def _bounded(name: str, value: float, low: float, high: float) -> None:
    if not low <= value <= high:
        raise SearchConfigurationError(f"{name} must be between {low:g} and {high:g}; got {value}")


def build_settings(
    *,
    backend: str = "local_searxng",
    clio_web_search_url: str = "",
    auto_install: bool = True,
    port: int = 18890,
    engines: list[str] | tuple[str, ...] = DEFAULT_ENGINES,
    opt_in_engines: list[str] | tuple[str, ...] = (),
    safe_search: int = 1,
    request_timeout_s: float = 10.0,
    max_results: int = 10,
    language: str = "en",
) -> SearchSettings:
    """Validate raw values into :class:`SearchSettings` (engine policy applied)."""

    chosen = backend.strip().casefold()
    if chosen not in BACKENDS:
        raise SearchConfigurationError(
            f"search.backend must be one of {', '.join(BACKENDS)}; got {backend!r}"
        )
    url = clio_web_search_url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise SearchConfigurationError("search.clio_web_search.url must be an http(s) URL")
    _bounded("search.searxng.port", port, 1024, 65535)
    _bounded("search.searxng.safe_search", safe_search, 0, 2)
    _bounded("search.searxng.request_timeout_s", request_timeout_s, 1, 60)
    _bounded("search.searxng.max_results", max_results, 1, 50)
    enabled, dropped = effective_engines(engines, opt_in_engines)
    if not enabled:
        raise SearchConfigurationError("search.searxng.engines enables no engine")
    notes = tuple(
        f"engine {name!r} is opt-in only; add it to search.searxng.opt_in_engines to enable it"
        for name in dropped
    )
    return SearchSettings(
        backend=cast(SearchBackendName, chosen),
        clio_web_search_url=url,
        auto_install=auto_install,
        port=int(port),
        engines=tuple(enabled),
        dropped_engines=tuple(dropped),
        safe_search=int(safe_search),
        request_timeout_s=float(request_timeout_s),
        max_results=int(max_results),
        language=language.strip() or "auto",
        notes=notes,
    )


def load_search_settings() -> SearchSettings:
    """Resolve ``search.*`` from config file, environment and defaults."""

    return build_settings(
        backend=conf.resolve(
            "search.backend", env="CLIO_SEARCH_BACKEND", default="local_searxng", cast=conf.as_str
        ),
        clio_web_search_url=conf.resolve(
            "search.clio_web_search.url",
            env="CLIO_SEARCH_CLIO_WEB_SEARCH_URL",
            default="",
            cast=conf.as_str,
        ),
        auto_install=conf.resolve(
            "search.local_searxng.auto_install",
            env="CLIO_SEARCH_AUTO_INSTALL",
            default=True,
            cast=conf.as_bool,
        ),
        port=conf.resolve(
            "search.searxng.port", env="CLIO_SEARCH_SEARXNG_PORT", default=18890, cast=conf.as_int
        ),
        engines=conf.resolve(
            "search.searxng.engines",
            env="CLIO_SEARCH_SEARXNG_ENGINES",
            default=DEFAULT_ENGINE_LIST,
            cast=conf.as_csv,
        ),
        opt_in_engines=conf.resolve(
            "search.searxng.opt_in_engines",
            env="CLIO_SEARCH_SEARXNG_OPT_IN_ENGINES",
            default=[],
            cast=conf.as_csv,
        ),
        safe_search=conf.resolve(
            "search.searxng.safe_search",
            env="CLIO_SEARCH_SEARXNG_SAFE_SEARCH",
            default=1,
            cast=conf.as_int,
        ),
        request_timeout_s=conf.resolve(
            "search.searxng.request_timeout_s",
            env="CLIO_SEARCH_SEARXNG_REQUEST_TIMEOUT_S",
            default=10.0,
            cast=conf.as_float,
        ),
        max_results=conf.resolve(
            "search.searxng.max_results",
            env="CLIO_SEARCH_SEARXNG_MAX_RESULTS",
            default=10,
            cast=conf.as_int,
        ),
        language=conf.resolve(
            "search.searxng.language",
            env="CLIO_SEARCH_SEARXNG_LANGUAGE",
            default="en",
            cast=conf.as_str,
        ),
    )
