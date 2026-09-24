"""One generic fetch -> disk-cache -> validate -> last-good pipeline for a named catalog.

Three call sites used to hand-roll their own version of this: models.dev's context
window catalog, the LiteLLM community model-cost map, and the maintained Claude Code
model catalog. Each had its own cache file, its own TTL logic, and its own (different)
answer for "what happens when the fetch fails". This module replaces all three private
implementations with one class, so a fix to the caching policy lands once.

Cache doctrine (see project ``CLAUDE.md``): a cache accelerates, it is never the
truth, and going stale is always a *typed* fact, never a silent one. Concretely:

* The disk cache lives at ``paths.user_cache_dir() / "catalogs" / "<name>.json"``,
  written atomically (temp file + ``os.replace``) so a crash mid-write can never
  leave a torn/partial cache behind.
* Every read returns a :class:`CatalogResult`, which carries the data PLUS its
  provenance (``source``, ``etag``/``version``, ``fetched_at``) and, when the data
  is not a fresh live fetch, a non-empty ``stale_reason`` explaining why (a failed
  fetch, a failed validation, disabled fetching, or a bundled cold start). Nothing
  here ever substitutes degraded data without saying so.
* A failed fetch or a failed validation of NEWLY fetched data never touches the
  existing disk cache: the last good copy rides through untouched, and the caller
  is told (via ``stale_reason``) that today's answer is not fresh.
* A conditional GET (``If-None-Match``) is used whenever the cache carries an
  ETag; a ``304`` only refreshes ``fetched_at`` (the payload didn't change, so
  there is nothing new to validate or write).
* The optional ``bundled`` loader is a last resort, used ONLY when there is no
  disk cache at all (a true cold start) and either fetching is disabled or the
  live fetch also failed. Its result is always tagged ``source="bundled"``.

Thread safety: ``FetchedCatalog.get`` takes a plain :class:`threading.Lock` around
each refresh. Every known caller in this codebase is synchronous OR reaches this
module through ``asyncio.to_thread`` (a real OS thread from the default executor),
so a ``threading.Lock`` correctly serializes concurrent refreshes from either kind
of caller -- there is no in-process ``asyncio`` concurrency to protect against here.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Generic, TypeVar

import httpx

from clio_agent import paths

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Generic defaults; every call site may override per its own data shape.
DEFAULT_TIMEOUT_S = 8.0
DEFAULT_MAX_BYTES = 8 * 1024 * 1024


def catalogs_dir() -> Path:
    """The shared disk-cache directory every :class:`FetchedCatalog` writes under."""
    return paths.user_cache_dir() / "catalogs"


def cache_path_for(name: str) -> Path:
    """The disk-cache file path for a catalog named ``name``."""
    return catalogs_dir() / f"{name}.json"


class FetchedCatalogUnavailable(RuntimeError):
    """No usable data anywhere: no fresh/stale disk cache, no bundled source, and
    (when fetching was allowed) the live fetch also failed. Callers translate this
    into their own domain error, or treat it as a plain miss -- this module never
    decides that policy itself."""


@dataclass(frozen=True)
class CatalogResult(Generic[T]):
    """One resolved read of a fetched catalog, with full provenance.

    Attributes:
        data: The parsed, validated catalog payload.
        source: Where ``data`` came from: ``"network"`` (a live fetch just
            happened, including a ``304`` confirming the cache), ``"disk_cache"``
            (served from the local cache, fresh or stale), or ``"bundled"`` (the
            packaged cold-start fallback).
        source_url: The URL this catalog fetches from.
        etag: The upstream ``ETag`` backing ``data``, or ``""`` when none was
            recorded (e.g. the bundled fallback, or an upstream that sends none).
        version: A content version that survives a reproducible re-lookup:
            ``"etag:<value>"`` when an ETag is available, else
            ``"sha256:<hex>"`` of the raw payload, or ``"bundled"``.
        fetched_at: ISO-8601 UTC timestamp of the evidence backing ``data`` (the
            last time this exact payload was confirmed live, not the time of
            this particular read).
        stale_reason: Empty when ``data`` is a fresh live fetch. Otherwise a
            short typed reason ``data`` is NOT fresh (e.g. ``"transport_error:
            ..."``, ``"validation_failed: ..."``, ``"fetch_disabled"``,
            ``"bundled_cold_start"``).
    """

    data: T
    source: str
    source_url: str
    etag: str
    version: str
    fetched_at: str
    stale_reason: str = ""


@dataclass(frozen=True)
class _CacheEntry:
    """The on-disk cache envelope: provenance metadata plus the raw fetched text."""

    fetched_at: str
    etag: str
    source_url: str
    version: str
    payload: str

    def to_json(self) -> dict[str, str]:
        return {
            "fetched_at": self.fetched_at,
            "etag": self.etag,
            "source_url": self.source_url,
            "version": self.version,
            "payload": self.payload,
        }

    @staticmethod
    def from_json(raw: object) -> "_CacheEntry | None":
        if not isinstance(raw, dict):
            return None
        try:
            return _CacheEntry(
                fetched_at=str(raw["fetched_at"]),
                etag=str(raw.get("etag") or ""),
                source_url=str(raw.get("source_url") or ""),
                version=str(raw.get("version") or ""),
                payload=str(raw["payload"]),
            )
        except (KeyError, TypeError):
            return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_s(iso_ts: str) -> float:
    """Seconds since ``iso_ts``, or ``+inf`` for an unparseable timestamp (treat as stale)."""
    try:
        parsed = datetime.fromisoformat(iso_ts)
    except ValueError:
        return float("inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _content_version(payload: bytes, etag: str) -> str:
    if etag:
        return f"etag:{etag}"
    return f"sha256:{sha256(payload).hexdigest()[:16]}"


class FetchedCatalog(Generic[T]):
    """Fetch + disk-cache (TTL, ETag) + validate + last-good, for one named catalog.

    Args:
        name: The catalog's cache-file stem (``catalogs/<name>.json``); also the
            identity used in every log line this instance emits.
        url: The URL to fetch the raw catalog document from.
        parse: Validates and parses raw response/cache bytes into ``T``. Must
            raise on anything invalid (any exception is treated as a validation
            failure, logged, and never substituted silently).
        ttl_s: How long a disk-cached copy is served without re-fetching.
        max_bytes: A fetched response larger than this is treated as a fetch
            failure (never parsed, never cached).
        timeout_s: Per-attempt HTTP timeout.
        bundled: Optional zero-arg loader for a packaged cold-start fallback,
            used ONLY when there is no disk cache at all and no live data.
        cache_path: Override the disk-cache location (tests).
    """

    def __init__(
        self,
        name: str,
        url: str,
        *,
        parse: Callable[[bytes], T],
        ttl_s: float,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        bundled: Callable[[], T] | None = None,
        cache_path: Path | None = None,
    ) -> None:
        if not name.strip():
            raise ValueError("FetchedCatalog requires a non-empty name")
        self.name = name
        self.url = url
        self._parse = parse
        self.ttl_s = ttl_s
        self.max_bytes = max_bytes
        self.timeout_s = timeout_s
        self._bundled = bundled
        self._explicit_cache_path = cache_path
        self._lock = threading.Lock()

    @property
    def cache_path(self) -> Path:
        """The disk-cache file path.

        Re-resolved from :func:`cache_path_for` on every access when no
        explicit ``cache_path`` was given at construction -- NOT baked in once
        -- because :func:`clio_agent.paths.user_cache_dir` is environment/
        config-sensitive (``CLIO_USER_DIR``, per-test isolation). A module-level
        singleton :class:`FetchedCatalog` (built once via ``lru_cache`` so every
        caller shares its refresh lock) must keep tracking the CURRENT user dir,
        not whichever one happened to be active the first time it was built.
        """
        return self._explicit_cache_path or cache_path_for(self.name)

    def get(self, *, force_refresh: bool = False, allow_fetch: bool = True) -> CatalogResult[T]:
        """Return this catalog's current data plus provenance.

        Args:
            force_refresh: Skip the TTL freshness check and attempt a live fetch
                even if the disk cache is still fresh (still uses the cache's
                ETag for a conditional GET).
            allow_fetch: When ``False``, never touch the network -- disk cache
                or bundled data only. Used for read-only accessors that must
                never block on I/O beyond the local disk.

        Returns:
            A :class:`CatalogResult`.

        Raises:
            FetchedCatalogUnavailable: No disk cache, no bundled source, and
                (when ``allow_fetch``) the live fetch also failed.
        """
        with self._lock:
            return self._get_locked(force_refresh=force_refresh, allow_fetch=allow_fetch)

    # -- internals -----------------------------------------------------------

    def _get_locked(self, *, force_refresh: bool, allow_fetch: bool) -> CatalogResult[T]:
        cached = self._read_disk_cache()

        if cached is not None and not force_refresh and _age_s(cached.fetched_at) < self.ttl_s:
            data = self._safe_parse(cached, context="disk_cache")
            if data is not None:
                return CatalogResult(
                    data=data,
                    source="disk_cache",
                    source_url=cached.source_url,
                    etag=cached.etag,
                    version=cached.version,
                    fetched_at=cached.fetched_at,
                )
            # The fresh-enough cache no longer parses (schema drift on disk) --
            # treat it as absent for the rest of this read.
            cached = None

        fetch_failure_reason = "fetch_disabled"
        if allow_fetch:
            fetched, fetch_failure_reason = self._fetch(cached)
            if fetched is not None:
                return fetched

        if cached is not None:
            data = self._safe_parse(cached, context="stale_fallback")
            if data is not None:
                logger.warning(
                    "fetched_catalog: reason=%s name=%s serving disk cache from %s",
                    fetch_failure_reason,
                    self.name,
                    cached.fetched_at,
                )
                return CatalogResult(
                    data=data,
                    source="disk_cache",
                    source_url=cached.source_url,
                    etag=cached.etag,
                    version=cached.version,
                    fetched_at=cached.fetched_at,
                    stale_reason=fetch_failure_reason,
                )

        if self._bundled is not None:
            try:
                data = self._bundled()
            except Exception as exc:  # noqa: BLE001 - a bad bundled loader is just "no bundled data"
                logger.warning(
                    "fetched_catalog: reason=bundled_load_failed name=%s: %s", self.name, exc
                )
            else:
                logger.warning(
                    "fetched_catalog: reason=bundled_cold_start name=%s "
                    "(no disk cache; fetch=%s) using the packaged snapshot",
                    self.name,
                    fetch_failure_reason if allow_fetch else "disabled",
                )
                return CatalogResult(
                    data=data,
                    source="bundled",
                    source_url=self.url,
                    etag="",
                    version="bundled",
                    fetched_at=_now_iso(),
                    stale_reason="bundled_cold_start",
                )

        raise FetchedCatalogUnavailable(
            f"{self.name}: no live fetch ({fetch_failure_reason}), no cached copy, "
            "no bundled source available"
        )

    def _fetch(self, cached: _CacheEntry | None) -> tuple[CatalogResult[T] | None, str]:
        """Attempt one live fetch. Returns ``(result_or_None, reason_if_None)``."""
        headers = {"If-None-Match": cached.etag} if cached and cached.etag else {}
        try:
            response = httpx.get(
                self.url, timeout=self.timeout_s, headers=headers, follow_redirects=True
            )
        except httpx.HTTPError as exc:
            reason = f"transport_error: {exc}"
            logger.warning(
                "fetched_catalog: reason=transport_error name=%s url=%s: %s",
                self.name,
                self.url,
                exc,
            )
            return None, reason

        if response.status_code == 304:
            if cached is None:
                reason = "not_modified_without_cache"
                logger.warning(
                    "fetched_catalog: reason=%s name=%s url=%s", reason, self.name, self.url
                )
                return None, reason
            refreshed = replace(cached, fetched_at=_now_iso())
            data = self._safe_parse(refreshed, context="network_304")
            if data is None:
                return None, "validation_failed: cached payload no longer valid on 304"
            self._write_disk_cache(refreshed)
            return (
                CatalogResult(
                    data=data,
                    source="network",
                    source_url=self.url,
                    etag=refreshed.etag,
                    version=refreshed.version,
                    fetched_at=refreshed.fetched_at,
                ),
                "",
            )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            reason = f"http_status_{response.status_code}"
            logger.warning(
                "fetched_catalog: reason=%s name=%s url=%s: %s", reason, self.name, self.url, exc
            )
            return None, reason

        payload = response.content
        if len(payload) > self.max_bytes:
            reason = f"too_large: {len(payload)} bytes > {self.max_bytes}"
            logger.warning(
                "fetched_catalog: reason=too_large name=%s url=%s bytes=%d limit=%d",
                self.name,
                self.url,
                len(payload),
                self.max_bytes,
            )
            return None, reason

        etag_value = response.headers.get("etag") or ""
        entry = _CacheEntry(
            fetched_at=_now_iso(),
            etag=etag_value,
            source_url=self.url,
            version=_content_version(payload, etag_value),
            payload=payload.decode("utf-8", errors="replace"),
        )
        data = self._safe_parse(entry, context="network")
        if data is None:
            return None, "validation_failed"  # disk cache is untouched -- see docstring
        self._write_disk_cache(entry)
        return (
            CatalogResult(
                data=data,
                source="network",
                source_url=self.url,
                etag=entry.etag,
                version=entry.version,
                fetched_at=entry.fetched_at,
            ),
            "",
        )

    def _safe_parse(self, entry: _CacheEntry, *, context: str) -> T | None:
        try:
            return self._parse(entry.payload.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - any parser failure is a typed validation failure
            logger.warning(
                "fetched_catalog: reason=validation_failed name=%s context=%s: %s",
                self.name,
                context,
                exc,
            )
            return None

    def _read_disk_cache(self) -> _CacheEntry | None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return _CacheEntry.from_json(raw)

    def _write_disk_cache(self, entry: _CacheEntry) -> None:
        try:
            cache_path = self.cache_path
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
            tmp.write_text(json.dumps(entry.to_json()), encoding="utf-8")
            tmp.replace(cache_path)
        except OSError as exc:
            logger.warning("fetched_catalog: reason=cache_write_failed name=%s: %s", self.name, exc)


__all__ = [
    "CatalogResult",
    "FetchedCatalog",
    "FetchedCatalogUnavailable",
    "cache_path_for",
    "catalogs_dir",
]
