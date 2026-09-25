"""``GET /v1/system/latest-release`` — the latest published CLIO release.

The web client cannot fetch a GitHub release asset directly: GitHub sends no
CORS headers on ``…/releases/latest/download/latest-lite.json``, so a browser
fetch is blocked and the version panel would sit on "Checking…" forever. The
connected CLIO server does the fetch instead — same-origin from the client's
point of view — and hands back just the version string the panel needs.

This is the SAME manifest the desktop Tauri updater plugin polls
(``plugins.updater.endpoints`` in the CLIO-branded ``clio-bundles.yml``
release workflow); the URL is configurable (never hardcoded past the
default) so a fork/rebrand publishing under its own repo overrides it rather
than silently reading iowarp/clio-agent's releases.

Cached with a TTL (process-wide, one entry) so repeated client polls (e.g.
reopening the version panel) do not re-hit GitHub on every render. Every
outcome — success or failure to fetch/parse the manifest — is a typed 200
response; there is no partial "hang" state, so a client-side ``useQuery``
always settles instead of parking on "checking" indefinitely.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import httpx
from pydantic import BaseModel

from clio_agent import conf

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_DEFAULT_MANIFEST_URL = (
    "https://github.com/iowarp/clio-agent/releases/latest/download/latest-lite.json"
)
_CACHE_TTL_S = 300.0  # 5 minutes -- matches the web client's own query staleTime.
_FETCH_TIMEOUT_S = 5.0


class LatestReleaseDegradation(BaseModel):
    """A typed, queryable reason the manifest fetch didn't produce a version."""

    reason: str
    message: str


class LatestReleaseResponse(BaseModel):
    """The version panel's one question, answered honestly.

    ``version`` is ``None`` -- never a guess -- whenever ``degradation`` is
    set: no manifest reachable, or a manifest with nothing usable in it.
    """

    version: Optional[str] = None
    source: str
    checked_at: str
    degradation: Optional[LatestReleaseDegradation] = None


def _manifest_url() -> str:
    return conf.resolve(
        "system.release_manifest_url",
        env="CLIO_RELEASE_MANIFEST_URL",
        default=_DEFAULT_MANIFEST_URL,
        cast=str,
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _Cache:
    """Process-wide TTL cache for the last fetch (success OR failure)."""

    def __init__(self) -> None:
        self._at = 0.0
        self._response: Optional[LatestReleaseResponse] = None

    def get(self, url: str, ttl_s: float) -> Optional[LatestReleaseResponse]:
        fresh = (time.monotonic() - self._at) < ttl_s
        if fresh and self._response is not None and self._response.source == url:
            return self._response
        return None

    def set(self, response: LatestReleaseResponse) -> None:
        self._response = response
        self._at = time.monotonic()


_CACHE = _Cache()


async def _fetch_latest_release(url: str) -> LatestReleaseResponse:
    """Fetch + parse the manifest, never raising -- every path is a typed 200."""

    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.info(
            "latest-release manifest unreachable reason=manifest_unreachable url=%s error=%s",
            url,
            exc,
        )
        return LatestReleaseResponse(
            source=url,
            checked_at=_iso_now(),
            degradation=LatestReleaseDegradation(
                reason="manifest_unreachable", message=str(exc) or type(exc).__name__
            ),
        )
    if response.status_code != 200:
        return LatestReleaseResponse(
            source=url,
            checked_at=_iso_now(),
            degradation=LatestReleaseDegradation(
                reason="manifest_unreachable",
                message=f"release manifest returned HTTP {response.status_code}",
            ),
        )
    try:
        manifest = response.json()
    except ValueError as exc:
        return LatestReleaseResponse(
            source=url,
            checked_at=_iso_now(),
            degradation=LatestReleaseDegradation(
                reason="manifest_unparsable", message=str(exc)
            ),
        )
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not version.strip():
        return LatestReleaseResponse(
            source=url,
            checked_at=_iso_now(),
            degradation=LatestReleaseDegradation(
                reason="manifest_missing_version",
                message="release manifest had no usable 'version' field",
            ),
        )
    return LatestReleaseResponse(version=version, source=url, checked_at=_iso_now())


def register_latest_release_routes(app: "FastAPI") -> None:
    """Register ``GET /v1/system/latest-release``."""

    @app.get("/v1/system/latest-release", response_model=LatestReleaseResponse)
    async def get_latest_release() -> LatestReleaseResponse:
        url = _manifest_url()
        cached = _CACHE.get(url, _CACHE_TTL_S)
        if cached is not None:
            return cached
        result = await _fetch_latest_release(url)
        _CACHE.set(result)
        return result


__all__ = ["LatestReleaseResponse", "register_latest_release_routes"]
