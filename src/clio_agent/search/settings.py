"""The ``search.*`` configuration: which sources serve web and literature search.

CLIO searches through hosted services, never a local search server:

* **web sources** (:data:`WEB_VENDORS`) answer ``web_search``. They are tried in the
  person's order (``search.web.order``); the first enabled, approved source that
  is not cooling down answers. When its free allowance runs out CLIO asks the
  person whether to switch to the next one (``search.web.auto_switch`` skips the
  question). A ``service`` source (``search.service.url``) is a CLIO search
  service a site or IOWarp runs; it goes first when configured.
* **literature sources** (:data:`LITERATURE_SOURCES`) answer ``literature_search``
  and ``literature_lookup`` together; ``search.literature.disabled`` turns some off.

Every source works without a key except the ones in :data:`KEY_REQUIRED`. A key a
person adds (:func:`key_ref` in the provider key store) raises the allowance and
moves nothing by itself. Sending queries to a source needs the person's approval
once (``search.approved``), asked through a question the first time it is used.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from clio_agent import conf

#: Web sources in their default order: the ones that filter by date and domain
#: without a key first, the lower-quality free tier last.
WEB_VENDORS: tuple[str, ...] = ("keenable", "exa", "firecrawl", "you", "parallel")
#: The CLIO search service source (``search.service.url``).
SERVICE_SOURCE = "service"
#: Literature sources queried by ``literature_search`` / ``literature_lookup``.
LITERATURE_SOURCES: tuple[str, ...] = (
    "openalex",
    "crossref",
    "arxiv",
    "chemrxiv",
    "europepmc",
    "osti",
    "opencitations",
    "datacite",
    "semantic_scholar",
)
#: Sources that answer nothing without a key the person added.
KEY_REQUIRED: frozenset[str] = frozenset({"semantic_scholar"})
#: The approval id that covers every literature source (one question, not eight).
LITERATURE_APPROVAL = "literature"

#: Human names, shown in questions and settings.
DISPLAY_NAMES: dict[str, str] = {
    "keenable": "Keenable",
    "exa": "Exa",
    "firecrawl": "Firecrawl",
    "you": "You.com",
    "parallel": "Parallel",
    SERVICE_SOURCE: "CLIO search service",
    "openalex": "OpenAlex",
    "crossref": "Crossref",
    "chemrxiv": "ChemRxiv",
    "arxiv": "arXiv",
    "europepmc": "Europe PMC",
    "osti": "OSTI.gov",
    "opencitations": "OpenCitations",
    "datacite": "DataCite",
    "semantic_scholar": "Semantic Scholar",
    LITERATURE_APPROVAL: "the literature sources",
}


class SearchConfigurationError(ValueError):
    """A ``search.*`` value CLIO cannot use; the message names the key and the fix."""

    code = "search_configuration_invalid"


def key_ref(source: str) -> str:
    """The provider-key-store id that holds a person's key for ``source``."""

    return f"search-provider:{source}"


@dataclass(frozen=True)
class SearchSettings:
    """The resolved ``search.*`` configuration."""

    enabled: bool = True
    web_order: tuple[str, ...] = WEB_VENDORS
    web_disabled: frozenset[str] = frozenset()
    auto_switch: bool = False
    approved: frozenset[str] = frozenset()
    literature_disabled: frozenset[str] = frozenset()
    paid: frozenset[str] = frozenset()
    service_url: str = ""
    contact_email: str = ""
    request_timeout_s: float = 20.0
    snippet_chars: int = 300
    max_results: int = 8

    def web_sources(self) -> tuple[str, ...]:
        """Enabled web sources in the order they are tried (the service first)."""

        order = [name for name in self.web_order if name not in self.web_disabled]
        if self.service_url and SERVICE_SOURCE not in self.web_disabled:
            order = [SERVICE_SOURCE, *[name for name in order if name != SERVICE_SOURCE]]
        else:
            order = [name for name in order if name != SERVICE_SOURCE]
        return tuple(order)

    def literature_sources(self, *, has_key: Mapping[str, bool] | None = None) -> tuple[str, ...]:
        """Enabled literature sources; a key-only source counts only with its key."""

        keys = has_key or {}
        return tuple(
            name
            for name in LITERATURE_SOURCES
            if name not in self.literature_disabled
            and (name not in KEY_REQUIRED or keys.get(name, False))
        )

    def is_approved(self, source: str) -> bool:
        """Whether the person approved sending queries to ``source``.

        The configured service is the person's own choice, so it needs no question.
        """

        return source == SERVICE_SOURCE or source in self.approved


def _names(key: str, values: Iterable[str], allowed: Iterable[str]) -> tuple[str, ...]:
    known = set(allowed)
    names: list[str] = []
    for raw in values:
        name = str(raw).strip().casefold()
        if not name:
            continue
        if name not in known:
            raise SearchConfigurationError(
                f"{key} names unknown source {name!r}; known sources: {', '.join(sorted(known))}"
            )
        if name not in names:
            names.append(name)
    return tuple(names)


def _bounded(key: str, value: float, low: float, high: float) -> None:
    if not low <= value <= high:
        raise SearchConfigurationError(f"{key} must be between {low:g} and {high:g}; got {value}")


def build_settings(
    *,
    enabled: bool = True,
    web_order: Iterable[str] = WEB_VENDORS,
    web_disabled: Iterable[str] = (),
    auto_switch: bool = False,
    approved: Iterable[str] = (),
    literature_disabled: Iterable[str] = (),
    paid: Iterable[str] = (),
    service_url: str = "",
    contact_email: str = "",
    request_timeout_s: float = 20.0,
    snippet_chars: int = 300,
    max_results: int = 8,
) -> SearchSettings:
    """Validate raw values into :class:`SearchSettings`.

    Sources missing from ``search.web.order`` keep their default position after the
    listed ones, so a new source added to CLIO is never silently unreachable.
    """

    web_known = (*WEB_VENDORS, SERVICE_SOURCE)
    order = _names("search.web.order", web_order, web_known)
    order = (*order, *[name for name in WEB_VENDORS if name not in order])
    url = service_url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        raise SearchConfigurationError("search.service.url must be an http(s) URL")
    email = contact_email.strip()
    if email and ("@" not in email or " " in email):
        raise SearchConfigurationError("search.contact_email must be an email address")
    _bounded("search.request_timeout_s", request_timeout_s, 2, 120)
    _bounded("search.snippet_chars", snippet_chars, 80, 4000)
    _bounded("search.max_results", max_results, 1, 50)
    return SearchSettings(
        enabled=bool(enabled),
        web_order=order,
        web_disabled=frozenset(_names("search.web.disabled", web_disabled, web_known)),
        auto_switch=bool(auto_switch),
        approved=frozenset(
            _names(
                "search.approved",
                approved,
                (*web_known, *LITERATURE_SOURCES, LITERATURE_APPROVAL),
            )
        ),
        literature_disabled=frozenset(
            _names("search.literature.disabled", literature_disabled, LITERATURE_SOURCES)
        ),
        paid=frozenset(_names("search.paid", paid, (*web_known, *LITERATURE_SOURCES))),
        service_url=url,
        contact_email=email,
        request_timeout_s=float(request_timeout_s),
        snippet_chars=int(snippet_chars),
        max_results=int(max_results),
    )


def load_search_settings() -> SearchSettings:
    """Resolve ``search.*`` from config files, environment and defaults."""

    no_names: list[str] = []
    return build_settings(
        enabled=conf.resolve(
            "search.enabled", env="CLIO_SEARCH_ENABLED", default=True, cast=conf.as_bool
        ),
        web_order=conf.resolve(
            "search.web.order",
            env="CLIO_SEARCH_WEB_ORDER",
            default=list(WEB_VENDORS),
            cast=conf.as_csv,
        ),
        web_disabled=conf.resolve(
            "search.web.disabled",
            env="CLIO_SEARCH_WEB_DISABLED",
            default=no_names,
            cast=conf.as_csv,
        ),
        auto_switch=conf.resolve(
            "search.web.auto_switch",
            env="CLIO_SEARCH_WEB_AUTO_SWITCH",
            default=False,
            cast=conf.as_bool,
        ),
        approved=conf.resolve(
            "search.approved", env="CLIO_SEARCH_APPROVED", default=no_names, cast=conf.as_csv
        ),
        literature_disabled=conf.resolve(
            "search.literature.disabled",
            env="CLIO_SEARCH_LITERATURE_DISABLED",
            default=no_names,
            cast=conf.as_csv,
        ),
        paid=conf.resolve(
            "search.paid", env="CLIO_SEARCH_PAID", default=no_names, cast=conf.as_csv
        ),
        service_url=conf.resolve(
            "search.service.url", env="CLIO_SEARCH_SERVICE_URL", default="", cast=conf.as_str
        ),
        contact_email=conf.resolve(
            "search.contact_email", env="CLIO_SEARCH_CONTACT_EMAIL", default="", cast=conf.as_str
        ),
        request_timeout_s=conf.resolve(
            "search.request_timeout_s",
            env="CLIO_SEARCH_REQUEST_TIMEOUT_S",
            default=20.0,
            cast=conf.as_float,
        ),
        snippet_chars=conf.resolve(
            "search.snippet_chars", env="CLIO_SEARCH_SNIPPET_CHARS", default=300, cast=conf.as_int
        ),
        max_results=conf.resolve(
            "search.max_results", env="CLIO_SEARCH_MAX_RESULTS", default=8, cast=conf.as_int
        ),
    )


def settings_document(settings: SearchSettings) -> dict[str, Any]:
    """The settings as the ``search`` mapping of the user ``config.yaml``."""

    return {
        "enabled": settings.enabled,
        "web": {
            "order": list(settings.web_order),
            "disabled": sorted(settings.web_disabled),
            "auto_switch": settings.auto_switch,
        },
        "approved": sorted(settings.approved),
        "literature": {"disabled": sorted(settings.literature_disabled)},
        "paid": sorted(settings.paid),
        "service": {"url": settings.service_url},
        "contact_email": settings.contact_email,
    }


__all__ = [
    "DISPLAY_NAMES",
    "KEY_REQUIRED",
    "LITERATURE_APPROVAL",
    "LITERATURE_SOURCES",
    "SERVICE_SOURCE",
    "WEB_VENDORS",
    "SearchConfigurationError",
    "SearchSettings",
    "build_settings",
    "key_ref",
    "load_search_settings",
    "settings_document",
]
