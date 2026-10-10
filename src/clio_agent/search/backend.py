"""The one search-backend abstraction behind CLIO's ``web_search`` tool.

``search.backend`` (:mod:`clio_agent.search.settings`) resolves to exactly one
:class:`SearchBackend`:

* :class:`LocalSearxngBackend` -- CLIO's private SearXNG on this computer's loopback
  (the managed ``searxng`` service), queried through its JSON API;
* :class:`ClioWebSearchBackend` -- a CLIO Web Search gateway (possibly on another
  machine), which speaks the same ``/search?format=json`` API;
* :class:`NoSearchBackend` -- search is off; every search raises the typed
  :class:`SearchNotConfiguredError` naming the fix.

The agent's ``web_search`` tool is the Web MCP server (``clio-kit mcp-server web`` /
``clio-web-search-mcp``). CLIO routes it here in two places, both through this
module: :func:`web_mcp_environment` points the server at the resolved backend when
it is spawned (an explicit ``--remote-url``/``WEB_*`` declaration still wins), and
:func:`web_search_guard` answers a call with the typed error instead of running it
when the resolved backend cannot serve (search off, the local SearXNG not
answering yet, or the gateway's ``/readyz`` failing). :meth:`SearchBackend.search`
is the direct path CLIO itself uses.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import PurePath
from typing import Any
from urllib.parse import urlsplit

import httpx

from clio_agent.search.settings import (
    SearchConfigurationError,
    SearchSettings,
    load_search_settings,
)

logger = logging.getLogger(__name__)

#: The tool CLIO's guard watches (Web MCP server ``web`` + tool ``search``).
WEB_SEARCH_TOOL = "web_search"
#: Web MCP environment that an explicit declaration may set; any of them keeps CLIO out.
WEB_MCP_VARIABLES = ("WEB_SEARCH_PROVIDER", "WEB_SEARXNG_BASE_URL", "WEB_REMOTE_URL")
_EXPLICIT_FLAGS = frozenset({"--remote-url", "--remote_url", "--address", "--provider"})
_WEB_MCP_COMMANDS = frozenset({"clio-web-search-mcp", "web-mcp"})

LocalEndpointResolver = Callable[[], "str | None"]
#: Lifecycle phase of the managed local SearXNG: ``starting``, ``stopped`` or ``None``.
LocalPhaseResolver = Callable[[], "str | None"]
_LOCAL_ENDPOINT: list[LocalEndpointResolver] = []
_LOCAL_PHASE: list[LocalPhaseResolver] = []
_ROUTING_LOCK = threading.Lock()
#: Whether the live Web MCP server was spawned routed by ``search.backend``.
_ROUTED_BY_BACKEND = {"value": False}
#: Gateway URL -> (monotonic time, last /readyz answer); a turn's calls share one probe.
_READYZ_CACHE: dict[str, tuple[float, "SearchNotConfiguredError | None"]] = {}
_READYZ_CACHE_S = 3.0
_READYZ_TIMEOUT_S = 2.0


class SearchNotConfiguredError(RuntimeError):
    """Web search cannot run with the current configuration; ``fix`` says what to change."""

    code = "search_not_configured"

    def __init__(self, detail: str, fix: str) -> None:
        super().__init__(f"{detail} Fix: {fix}")
        self.detail = detail
        self.fix = fix

    def as_tool_result(self) -> dict[str, Any]:
        """The model-facing result of a guarded ``web_search`` call."""

        return {"ok": False, "error": self.code, "detail": self.detail, "fix": self.fix}


class SearchBackendUnavailableError(SearchNotConfiguredError):
    """The configured backend exists but is not answering (yet)."""

    code = "search_backend_unavailable"


class SearchBackendStartingError(SearchBackendUnavailableError):
    """The backend is being installed or started; retrying shortly should work."""

    code = "search_backend_starting"


class SearchBackendStoppedError(SearchBackendUnavailableError):
    """The backend is installed but stopped; someone has to start it."""

    code = "search_backend_stopped"


def register_local_endpoint_resolver(
    resolver: LocalEndpointResolver | None, phase: LocalPhaseResolver | None = None
) -> None:
    """Install where the managed local SearXNG listens and its lifecycle phase.

    gact reads both from its service record and operations; ``phase`` answers
    ``starting`` (an install/start is in flight), ``stopped`` or ``None`` (unknown or
    failed), which picks the typed error a call gets while SearXNG is not answering.
    """

    _LOCAL_ENDPOINT.clear()
    _LOCAL_PHASE.clear()
    if resolver is not None:
        _LOCAL_ENDPOINT.append(resolver)
    if phase is not None:
        _LOCAL_PHASE.append(phase)


def _local_phase() -> str | None:
    for resolver in _LOCAL_PHASE:
        try:
            return resolver()
        except (OSError, ValueError, KeyError, AttributeError) as exc:
            logger.warning("local SearXNG phase unresolved reason=resolver_error: %r", exc)
    return None


def _normalized_hit(item: Mapping[str, Any]) -> dict[str, Any]:
    engines = item.get("engines")
    return {
        "title": str(item.get("title") or ""),
        "url": str(item.get("url") or ""),
        "snippet": str(item.get("content") or ""),
        "engines": [str(name) for name in engines] if isinstance(engines, list) else [],
    }


class SearchBackend:
    """One configured search service."""

    name = "base"

    def __init__(self, settings: SearchSettings) -> None:
        self.settings = settings

    def problem(self) -> SearchNotConfiguredError | None:
        """Why a search cannot run now (cheap: loopback checks only), or ``None``."""

        return None

    def readiness_problem(self) -> SearchNotConfiguredError | None:
        """:meth:`problem`, plus whether the backend answers now (what the guard asks)."""

        return self.problem()

    def web_mcp_environment(self) -> dict[str, str]:
        """Environment that points the Web MCP server at this backend."""

        return {}

    def search(
        self, query: str, count: int | None = None, *, client: httpx.Client | None = None
    ) -> dict[str, Any]:
        """Run one search; raise :class:`SearchNotConfiguredError` when it cannot."""

        raise NotImplementedError


class NoSearchBackend(SearchBackend):
    """``search.backend: none``."""

    name = "none"

    def problem(self) -> SearchNotConfiguredError:
        return SearchNotConfiguredError(
            "Web search is turned off (search.backend: none).",
            "Set search.backend to local_searxng (CLIO installs a private SearXNG on this "
            "computer) or to clio_web_search with search.clio_web_search.url, then retry.",
        )

    def search(
        self, query: str, count: int | None = None, *, client: httpx.Client | None = None
    ) -> dict[str, Any]:
        raise self.problem()


class SearxngJsonBackend(SearchBackend):
    """A backend that serves SearXNG's ``/search?format=json`` API at :meth:`base_url`."""

    def base_url(self) -> str:
        raise NotImplementedError

    def search(
        self, query: str, count: int | None = None, *, client: httpx.Client | None = None
    ) -> dict[str, Any]:
        problem = self.problem()
        if problem is not None:
            raise problem
        text = query.strip()
        if not text:
            raise ValueError("A non-empty query is required")
        limit = min(count or self.settings.max_results, self.settings.max_results)
        endpoint = f"{self.base_url()}/search"
        params: dict[str, str | int] = {
            "q": text,
            "format": "json",
            "safesearch": self.settings.safe_search,
        }
        if self.settings.language and self.settings.language != "auto":
            params["language"] = self.settings.language
        timeout = httpx.Timeout(self.settings.request_timeout_s + 10, connect=5.0)
        owned = client is None
        http = client or httpx.Client(timeout=timeout, trust_env=not self._loopback())
        try:
            response = http.get(endpoint, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchBackendUnavailableError(
                f"The {self.name} search backend at {self.base_url()} failed: "
                f"{type(exc).__name__}: {exc}.",
                self._fix(),
            ) from exc
        finally:
            if owned:
                http.close()
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SearchBackendUnavailableError(
                f"The {self.name} search backend returned malformed JSON.", self._fix()
            )
        results = [_normalized_hit(item) for item in rows if isinstance(item, dict)][:limit]
        unresponsive = [str(pair[0]) for pair in payload.get("unresponsive_engines") or [] if pair]
        return {
            "ok": True,
            "backend": self.name,
            "query": text,
            "results": results,
            "count": len(results),
            "unresponsive_engines": unresponsive,
        }

    def _loopback(self) -> bool:
        return urlsplit(self.base_url()).hostname in {"127.0.0.1", "localhost", "::1"}

    def _fix(self) -> str:
        return "Check the backend's logs, or choose another search.backend."


class LocalSearxngBackend(SearxngJsonBackend):
    """CLIO's managed SearXNG on this computer's loopback."""

    name = "local_searxng"

    def base_url(self) -> str:
        for resolver in _LOCAL_ENDPOINT:
            try:
                url = resolver()
            except (OSError, ValueError, KeyError, AttributeError) as exc:
                logger.warning("local SearXNG endpoint unresolved reason=resolver_error: %r", exc)
                url = None
            if url:
                return url.rstrip("/")
        return f"http://127.0.0.1:{self.settings.port}"

    def problem(self) -> SearchNotConfiguredError | None:
        parts = urlsplit(self.base_url())
        try:
            with socket.create_connection(
                (parts.hostname or "127.0.0.1", parts.port or 80), timeout=0.5
            ):
                return None
        except OSError:
            phase = _local_phase()
        if phase == "starting":
            return SearchBackendStartingError(
                f"CLIO's local SearXNG at {self.base_url()} is still being installed or started.",
                "Wait for the operation in Infrastructure > SearXNG to finish, then retry.",
            )
        if phase == "stopped":
            return SearchBackendStoppedError(
                f"CLIO's local SearXNG is installed but stopped ({self.base_url()}).",
                "Start it from Infrastructure > SearXNG, then retry. To search from another "
                "machine instead, set search.backend: clio_web_search and "
                "search.clio_web_search.url.",
            )
        return SearchBackendUnavailableError(
            f"CLIO's local SearXNG is not answering at {self.base_url()}.", self._fix()
        )

    def _fix(self) -> str:
        install = (
            "CLIO installs and starts it automatically on first run; follow the operation "
            "in Infrastructure > SearXNG and retry when it is running"
            if self.settings.auto_install
            else "Install and start it from Infrastructure > SearXNG "
            "(search.local_searxng.auto_install is off)"
        )
        return (
            f"{install}. To search from another machine instead, set search.backend: "
            "clio_web_search and search.clio_web_search.url."
        )

    def web_mcp_environment(self) -> dict[str, str]:
        # A host proxy (common on HPC) must not carry the server's loopback requests.
        bypass = [
            part.strip()
            for part in (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",")
            if part.strip()
        ]
        bypass += [host for host in ("127.0.0.1", "localhost", "::1") if host not in bypass]
        return {
            "WEB_SEARCH_PROVIDER": "searxng",
            "WEB_SEARXNG_BASE_URL": self.base_url(),
            "NO_PROXY": ",".join(bypass),
            "no_proxy": ",".join(bypass),
        }


class ClioWebSearchBackend(SearxngJsonBackend):
    """A CLIO Web Search gateway, local or on another machine."""

    name = "clio_web_search"

    def base_url(self) -> str:
        return self.settings.clio_web_search_url

    def problem(self) -> SearchNotConfiguredError | None:
        if self.settings.clio_web_search_url:
            return None
        return SearchNotConfiguredError(
            "search.backend is clio_web_search but search.clio_web_search.url is empty.",
            "Set search.clio_web_search.url to the gateway (for example "
            "http://search-host:8089), or set search.backend: local_searxng.",
        )

    def readiness_problem(self) -> SearchNotConfiguredError | None:
        problem = self.problem()
        if problem is not None:
            return problem
        url = self.base_url().rstrip("/")
        now = time.monotonic()
        with _ROUTING_LOCK:
            cached = _READYZ_CACHE.get(url)
        if cached is not None and now - cached[0] < _READYZ_CACHE_S:
            return cached[1]
        result = self._probe_readyz(url)
        with _ROUTING_LOCK:
            _READYZ_CACHE[url] = (now, result)
        return result

    def _probe_readyz(self, url: str) -> SearchNotConfiguredError | None:
        # The gateway listens before it can serve; its Web MCP server cannot even
        # start until /readyz passes (task-runtime discovery answers 502).
        try:
            with httpx.Client(
                timeout=httpx.Timeout(_READYZ_TIMEOUT_S), trust_env=not self._loopback()
            ) as http:
                response = http.get(f"{url}/readyz")
        except httpx.HTTPError as exc:
            return SearchBackendUnavailableError(
                f"The CLIO Web Search gateway at {url} is not answering ({type(exc).__name__}).",
                self._fix(),
            )
        if response.is_success:
            return None
        # It answers, so it is up but still preparing (or degraded): retrying helps.
        return SearchBackendStartingError(
            f"The CLIO Web Search gateway at {url} is not ready yet "
            f"(/readyz answered HTTP {response.status_code}).",
            self._fix(),
        )

    def _fix(self) -> str:
        return (
            f"Check that the CLIO Web Search gateway at {self.base_url()} is running and "
            "reachable from this computer, or set search.backend: local_searxng."
        )

    def web_mcp_environment(self) -> dict[str, str]:
        url = self.settings.clio_web_search_url
        return {"WEB_REMOTE_URL": url} if url else {}


def resolve_search_backend(settings: SearchSettings | None = None) -> SearchBackend:
    """The backend ``search.backend`` selects (settings read fresh when not given)."""

    chosen = settings or load_search_settings()
    if chosen.backend == "local_searxng":
        return LocalSearxngBackend(chosen)
    if chosen.backend == "clio_web_search":
        return ClioWebSearchBackend(chosen)
    return NoSearchBackend(chosen)


def is_web_search_mcp(command: str, args: Sequence[str]) -> bool:
    """Whether a stdio declaration launches the Web MCP server (any supported launcher)."""

    values = [str(value) for value in args]
    base = PurePath(command.replace("\\", "/")).name.casefold().removesuffix(".exe")
    if base in _WEB_MCP_COMMANDS:
        return True
    if any("web_mcp.server" in value for value in values):
        return True
    return any(
        first == "mcp-server" and second == "web"
        for first, second in zip(values, values[1:], strict=False)
    )


def web_mcp_environment(
    command: str,
    args: Sequence[str],
    spec_env: Mapping[str, str],
    *,
    settings: SearchSettings | None = None,
) -> dict[str, str]:
    """Backend environment for a Web MCP spawn; ``{}`` for any other server.

    A declaration that chose its own search endpoint (``--remote-url``, ``--address``,
    ``--provider`` or a ``WEB_*`` variable) is left exactly as declared.
    """

    if not is_web_search_mcp(command, args):
        return {}
    explicit = any(str(value).split("=", 1)[0] in _EXPLICIT_FLAGS for value in args) or any(
        name in spec_env for name in WEB_MCP_VARIABLES
    )
    if explicit:
        _set_routed(False)
        return {}
    try:
        backend = resolve_search_backend(settings)
    except SearchConfigurationError as exc:
        logger.error("web search not routed reason=%s: %s", exc.code, exc)
        _set_routed(False)
        return {}
    _set_routed(True)
    return backend.web_mcp_environment()


def _set_routed(value: bool) -> None:
    with _ROUTING_LOCK:
        _ROUTED_BY_BACKEND["value"] = value


def web_search_guard(
    name: str, args: Mapping[str, Any], *, settings: SearchSettings | None = None
) -> dict[str, Any] | None:
    """The typed error a ``web_search`` call returns instead of running, or ``None``.

    Only a Web MCP server CLIO routed by ``search.backend`` is guarded: an explicitly
    declared endpoint is the person's choice and reports its own errors.
    """

    del args
    if name != WEB_SEARCH_TOOL:
        return None
    with _ROUTING_LOCK:
        routed = _ROUTED_BY_BACKEND["value"]
    if not routed:
        return None
    try:
        problem = resolve_search_backend(settings).readiness_problem()
    except SearchConfigurationError as exc:
        problem = SearchNotConfiguredError(str(exc), "Correct the search.* configuration.")
    return problem.as_tool_result() if problem is not None else None
