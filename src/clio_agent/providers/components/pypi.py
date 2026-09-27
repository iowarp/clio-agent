"""Latest-release lookup for the user-updatable provider components (PyPI JSON API).

``GET <index>/<distribution>/json`` lists every release with its files. A
release counts as installable HERE only when it is final (no pre-release), and
has a non-yanked WHEEL whose tags this interpreter supports and whose
``requires_python`` it satisfies. The wheel requirement is load-bearing: an
sdist of ``claude-agent-sdk`` installs without the bundled ``claude`` binary
its transport runs, and 0.2.157/0.2.160 published no Windows wheel at all, so
"newest on PyPI" is not "newest this computer can run".

Results are cached per distribution for ``providers.component_updates.ttl_s``
(success AND failure, so a dead network is not re-asked on every panel open).
Every failure is a typed :class:`PyPILookupError` code, never a guessed version.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import Tag, sys_tags
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
from packaging.version import InvalidVersion, Version

from clio_agent import conf

DEFAULT_INDEX_URL = "https://pypi.org/pypi"
DEFAULT_TTL_S = 3600.0
_FETCH_TIMEOUT_S = 10.0


class PyPILookupError(RuntimeError):
    """A typed failure to learn a distribution's releases (``code`` is queryable)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@dataclass(frozen=True)
class WheelFile:
    """One downloadable wheel of a release."""

    filename: str
    url: str
    sha256: str


@dataclass(frozen=True)
class ReleaseIndex:
    """Every installable release of one distribution, newest first."""

    distribution: str
    installable: dict[str, WheelFile] = field(default_factory=dict)
    fetched_at: float = 0.0

    def versions(self) -> list[str]:
        """Installable versions, newest first."""
        return sorted(self.installable, key=Version, reverse=True)

    @property
    def latest(self) -> str:
        """The newest installable version, or ``""`` when none is."""
        ordered = self.versions()
        return ordered[0] if ordered else ""


def index_url() -> str:
    """The JSON API base (``providers.component_updates.index_url``)."""
    return conf.resolve(
        "providers.component_updates.index_url",
        env="CLIO_PROVIDER_COMPONENT_INDEX_URL",
        default=DEFAULT_INDEX_URL,
        cast=str,
    ).rstrip("/")


def ttl_s() -> float:
    """How long one lookup is served before PyPI is asked again."""
    return conf.resolve(
        "providers.component_updates.ttl_s",
        env="CLIO_PROVIDER_COMPONENT_TTL_S",
        default=DEFAULT_TTL_S,
        cast=conf.as_float,
    )


def _python_version() -> str:
    import platform  # noqa: PLC0415

    return platform.python_version()


def compatible_wheel(
    files: Iterable[dict[str, Any]],
    *,
    supported: frozenset[Tag],
    python_version: str,
) -> WheelFile | None:
    """The first non-yanked wheel among ``files`` this interpreter can install."""
    for entry in files:
        filename = str(entry.get("filename") or "")
        if not filename.endswith(".whl") or entry.get("yanked"):
            continue
        requires = str(entry.get("requires_python") or "").strip()
        if requires:
            try:
                if python_version not in SpecifierSet(requires):
                    continue
            except InvalidSpecifier:
                continue
        try:
            _, _, _, tags = parse_wheel_filename(filename)
        except InvalidWheelFilename:
            continue
        if tags & supported:
            digests = entry.get("digests") or {}
            return WheelFile(
                filename=filename,
                url=str(entry.get("url") or ""),
                sha256=str(digests.get("sha256") or ""),
            )
    return None


def parse_release_index(
    distribution: str,
    payload: dict[str, Any],
    *,
    supported: frozenset[Tag] | None = None,
    python_version: str | None = None,
    fetched_at: float = 0.0,
) -> ReleaseIndex:
    """Reduce a PyPI JSON document to the releases installable on this interpreter."""
    tags = supported if supported is not None else frozenset(sys_tags())
    py = python_version or _python_version()
    releases = payload.get("releases")
    if not isinstance(releases, dict):
        raise PyPILookupError("pypi_malformed", f"{distribution}: the index reply has no releases")
    installable: dict[str, WheelFile] = {}
    for version, files in releases.items():
        try:
            parsed = Version(str(version))
        except InvalidVersion:
            continue
        if parsed.is_prerelease or parsed.is_devrelease or not isinstance(files, list):
            continue
        wheel = compatible_wheel(files, supported=tags, python_version=py)
        if wheel is not None:
            installable[str(version)] = wheel
    return ReleaseIndex(distribution=distribution, installable=installable, fetched_at=fetched_at)


Fetcher = Callable[[str], dict[str, Any]]


def _http_fetch(url: str) -> dict[str, Any]:
    try:
        response = httpx.get(url, timeout=_FETCH_TIMEOUT_S, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise PyPILookupError("pypi_unreachable", f"{url}: {exc}") from exc
    if response.status_code == 404:
        raise PyPILookupError("pypi_not_found", f"{url}: 404")
    if response.status_code != 200:
        raise PyPILookupError("pypi_http_error", f"{url}: HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise PyPILookupError("pypi_malformed", f"{url}: {exc}") from exc
    if not isinstance(body, dict):
        raise PyPILookupError("pypi_malformed", f"{url}: not a JSON object")
    return body


class ReleaseLookup:
    """TTL-cached release lookup (one entry per distribution, success or failure)."""

    def __init__(
        self, fetch: Fetcher | None = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._fetch = fetch or _http_fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, ReleaseIndex | PyPILookupError]] = {}

    def clear(self) -> None:
        """Drop every cached answer."""
        with self._lock:
            self._entries.clear()

    def releases(self, distribution: str, *, refresh: bool = False) -> ReleaseIndex:
        """The installable releases of ``distribution`` (raises :class:`PyPILookupError`)."""
        now = self._clock()
        with self._lock:
            cached = self._entries.get(distribution)
        if cached is not None and not refresh and now - cached[0] < ttl_s():
            result = cached[1]
        else:
            try:
                payload = self._fetch(f"{index_url()}/{distribution}/json")
                result = parse_release_index(distribution, payload, fetched_at=time.time())
            except PyPILookupError as exc:
                result = exc
            with self._lock:
                self._entries[distribution] = (now, result)
        if isinstance(result, PyPILookupError):
            raise result
        return result


#: The process-wide lookup the status route and the updater share.
RELEASES = ReleaseLookup()


__all__ = [
    "DEFAULT_INDEX_URL",
    "PyPILookupError",
    "RELEASES",
    "ReleaseIndex",
    "ReleaseLookup",
    "WheelFile",
    "compatible_wheel",
    "index_url",
    "parse_release_index",
    "ttl_s",
]
