"""Fetch a pinned LibreOffice package from The Document Foundation's mirrors.

TDF's redirector sends each request to a random mirror, and one slow mirror can hold a
download for its whole budget. Its Metalink names the mirrors, size and SHA-256, so the
download races a few, keeps the winner's bytes, and resumes on the next mirror when the
current one falls below a floor. Every byte is still checked against the pinned SHA-256.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import queue
import random
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import urlsplit
from xml.etree import ElementTree

import truststore

if TYPE_CHECKING or __package__:
    from .process import DocumentError
else:
    from process import DocumentError

MAX_DOWNLOAD = 600 * 1024 * 1024
# TDF's redirector picks a random mirror per request, and one slow mirror (a few hundred
# KB/s from cloud hosts) can hold a download for the whole budget. Its Metalink lists the
# mirrors, size and SHA-256, so the download races a few of them and leaves one that slows.
_TDF = "https://download.documentfoundation.org/"
_TDF_ARCHIVE = "https://downloadarchive.documentfoundation.org/"
_METALINK = "{urn:ietf:params:xml:ns:metalink}"
_METALINK_MAX = 1024 * 1024
DOWNLOAD_BUDGET_S = 600.0
_RACE_MIRRORS = 3
_RACE_BYTES = 4 * 1024 * 1024
_RACE_WAIT_S = 20.0
_RACE_FALLBACKS = 8
# A mirror slower than this fraction of the race winner's rate over this window is left
# for the next one; the bytes already received are kept and the download resumes there.
FLOOR_FRACTION = 0.25
FLOOR_WINDOW_S = 10.0
_MAX_SWITCHES = 4
#: Failed attempts (5xx/408/429, network errors, a wrong SHA-256) before a download
#: fails. snapshot.ubuntu.com, which serves Office's ~150 userland packages, answers
#: bursts of 500/502 lasting several seconds; three attempts 1 s and 2 s apart failed
#: whole installs. Backoff doubles from 1 s (cap 16 s, ~31 s in all) with jitter, so
#: parallel fetches do not retry in lockstep.
_MAX_ATTEMPTS = 6
_BACKOFF_CAP_S = 16.0
_CHUNK = 1024 * 1024

logger = logging.getLogger(__name__)


class _RetryableDownloadError(DocumentError):
    """A network failure or unverified response permits a bounded fresh request."""


class _SlowMirror(Exception):
    """The current source fell below the throughput floor; resume on the next one."""

    def __init__(self, rate: float) -> None:
        super().__init__(f"{rate / 1e6:.2f} MB/s")
        self.rate = rate


def _archive_url(url: str) -> str:
    """TDF's own archive copy of a release file, under the full four-part version."""
    # The pinned release is build .2 of its version (26.2.6 is 26.2.6.2 in the archive).
    return re.sub(r"/stable/(\d+\.\d+\.\d+)/", r"/old/\1.2/", url.replace(_TDF, _TDF_ARCHIVE))


def _host(url: str) -> str:
    return urlsplit(url).netloc or url


def _open(url: str, start: int = 0, end: int | None = None, timeout: float = 30) -> BinaryIO:
    headers = {"Cache-Control": "no-cache", "Accept-Encoding": "identity"}
    if start or end is not None:
        headers["Range"] = f"bytes={start}-{'' if end is None else end}"
    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    request = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(request, timeout=timeout, context=context)


def _metalink(url: str, checksum: str) -> tuple[int, list[str]] | None:
    """TDF's size and mirrors (by its priority) for ``url``, when it vouches for the pin."""
    if not url.startswith(_TDF):
        return None
    try:
        with _open(url + ".meta4", timeout=10) as response:
            document = response.read(_METALINK_MAX + 1)
        if len(document) > _METALINK_MAX:
            raise ValueError("oversized Metalink")
        entry = ElementTree.fromstring(document).find(f"{_METALINK}file")
        if entry is None:
            raise ValueError("Metalink lists no file")
        hashes = {
            node.get("type"): (node.text or "").strip().lower()
            for node in entry.findall(f"{_METALINK}hash")
        }
        if hashes.get("sha-256") != checksum:
            raise ValueError("Metalink SHA-256 differs from the pin")
        size = int(entry.findtext(f"{_METALINK}size") or "")
        if not 0 < size <= MAX_DOWNLOAD:
            raise ValueError(f"Metalink size {size} is outside the download limit")
    except (OSError, http.client.HTTPException, ElementTree.ParseError, ValueError) as exc:
        logger.warning("reason=office_metalink_unavailable url=%s error=%s", url, exc)
        return None
    name = url.rsplit("/", 1)[-1]
    scheme = urlsplit(_TDF).scheme + "://"

    def priority(node: ElementTree.Element) -> int:
        value = node.get("priority") or ""
        return int(value) if value.isdigit() else 1 << 30

    mirrors: list[str] = []
    for node in sorted(entry.findall(f"{_METALINK}url"), key=priority):
        mirror = (node.text or "").strip()
        if (
            mirror.startswith(scheme)
            and mirror.rsplit("/", 1)[-1] == name
            and _host(mirror) != _host(_TDF)
            and mirror not in mirrors
        ):
            mirrors.append(mirror)
    return size, mirrors


def _race(
    candidates: list[str], size: int, deadline: float
) -> tuple[list[str], bytes, float] | None:
    """Fetch the first bytes from every candidate at once; the first to finish wins.

    Returns the finishers fastest first (failed and late ones dropped), the winner's bytes
    and its rate, measured from its response headers so connection setup does not count.
    """
    want = min(_RACE_BYTES, size)
    results: queue.Queue[tuple[str, bytes | None, float]] = queue.Queue()

    def probe(url: str) -> None:
        try:
            with _open(url, 0, want - 1, timeout=10) as response:
                if getattr(response, "status", None) != 206:
                    raise ValueError("range not honoured")
                opened = time.monotonic()
                data = response.read(want + 1)
                elapsed = time.monotonic() - opened
            if len(data) != want:
                raise ValueError(f"{len(data)} of {want} bytes")
            results.put((url, data, want / max(elapsed, 1e-3)))
        except (OSError, http.client.HTTPException, ValueError):
            results.put((url, None, 0.0))

    for url in candidates:
        threading.Thread(target=probe, args=(url,), name="office-mirror-race", daemon=True).start()
    began = time.monotonic()
    limit = min(began + _RACE_WAIT_S, deadline)
    rates: dict[str, float] = {}
    failed: set[str] = set()
    winner: tuple[str, bytes, float] | None = None
    while len(rates) + len(failed) < len(candidates):
        try:
            url, data, rate = results.get(timeout=max(limit - time.monotonic(), 0))
        except queue.Empty:
            break
        if data is None:
            failed.add(url)
            continue
        rates[url] = rate
        if winner is None:
            winner = (url, data, rate)
            # The others get as long again (at least 0.5 s) to be ranked for a switch.
            limit = min(limit, time.monotonic() + max(0.5, time.monotonic() - began))
    if winner is None:
        logger.warning(
            "reason=office_mirror_race_failed raced=%s", ",".join(map(_host, candidates))
        )
        return None
    ranked = [
        winner[0],
        *sorted((u for u in rates if u != winner[0]), key=rates.__getitem__, reverse=True),
    ]
    logger.info(
        "reason=office_mirror_race winner=%s mb_s=%.1f raced=%s",
        _host(winner[0]),
        winner[2] / 1e6,
        ",".join(
            f"{_host(u)}:{rates[u] / 1e6:.1f}"
            if u in rates
            else f"{_host(u)}:{'failed' if u in failed else 'late'}"
            for u in candidates
        ),
    )
    return ranked, winner[1], winner[2]


class _Partial:
    """The bytes received so far, hashed as written, so another source can resume them."""

    def __init__(self, output: BinaryIO) -> None:
        self.output = output
        self.digest = hashlib.sha256()
        self.count = 0

    def write(self, chunk: bytes) -> None:
        if self.count + len(chunk) > MAX_DOWNLOAD:
            raise DocumentError("LibreOffice download exceeds its size limit")
        self.digest.update(chunk)
        self.output.write(chunk)
        self.count += len(chunk)

    def restart(self) -> None:
        self.output.seek(0)
        self.output.truncate()
        self.digest = hashlib.sha256()
        self.count = 0


def _response_total(response: BinaryIO, status: int, offset: int) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    if status == 206:
        span = str(headers.get("Content-Range") or "")
        start, _, total = span.removeprefix("bytes ").partition("/")
        if not start.split("-")[0].isdigit() or int(start.split("-")[0]) != offset:
            raise _RetryableDownloadError(f"unexpected Content-Range {span!r} for offset {offset}")
        return int(total) if total.isdigit() else None
    length = str(headers.get("Content-Length") or "")
    return int(length) if length.isdigit() else None


def _stream(
    url: str,
    partial: _Partial,
    size: int | None,
    floor: float | None,
    deadline: float,
    mirror: bool,
) -> str:
    """Append ``url``'s bytes from ``partial.count``; returns the URL that served them.

    Raises ``_SlowMirror`` when the rate over the floor window drops below ``floor`` with
    more than a window's worth left, and ``_RetryableDownloadError`` for failures another
    attempt may fix. The bytes received so far stay, so the next source resumes them.
    """
    offset = partial.count
    try:
        try:
            response = _open(url, offset)
        except urllib.error.HTTPError as exc:
            if mirror or exc.code not in {404, 410} or not url.startswith(_TDF):
                raise
            url = _archive_url(url)
            response = _open(url, offset)
        with response:
            get_url = getattr(response, "geturl", None)
            served = str(get_url()) if callable(get_url) else url
            status = int(getattr(response, "status", None) or 200)
            if offset and status != 206:
                partial.restart()
            total = size or _response_total(response, status, partial.count)
            read = getattr(response, "read1", None) or response.read
            opened = time.monotonic()
            window = deque([(opened, partial.count)])
            while chunk := read(_CHUNK):
                partial.write(chunk)
                now = time.monotonic()
                if now > deadline:
                    raise DocumentError(
                        f"LibreOffice download exceeded {DOWNLOAD_BUDGET_S:.0f} seconds"
                    )
                if floor is None or now - window[-1][0] < 0.5:
                    continue
                window.append((now, partial.count))
                while len(window) > 1 and now - window[1][0] >= FLOOR_WINDOW_S:
                    window.popleft()
                if now - opened < FLOOR_WINDOW_S:
                    continue
                rate = (partial.count - window[0][1]) / max(now - window[0][0], 1e-3)
                left = (total - partial.count) if total else None
                if rate < floor and (left is None or left > rate * FLOOR_WINDOW_S):
                    raise _SlowMirror(rate)
    except urllib.error.HTTPError as exc:
        if exc.code == 416:
            partial.restart()
        retryable = mirror or exc.code in {408, 416, 429} or exc.code >= 500
        error_type = _RetryableDownloadError if retryable else DocumentError
        raise error_type(f"LibreOffice download failed: HTTP {exc.code} from {url}") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise _RetryableDownloadError(f"LibreOffice download failed: {exc} ({_host(url)})") from exc
    if total and partial.count < total:
        raise _RetryableDownloadError(
            f"LibreOffice download from {_host(served)} ended at {partial.count} of {total} bytes"
        )
    return served


def _sources(
    url: str, checksum: str, partial: _Partial, deadline: float
) -> tuple[list[str], int | None, float | None]:
    """Where to fetch from, best first, the expected size, and the throughput floor.

    A TDF URL whose Metalink vouches for the pin races its top mirrors and TDF's archive;
    the winner's bytes are kept. Otherwise (or if no racer finishes) it is ``url`` alone.
    """
    listing = _metalink(url, checksum)
    if listing is None:
        return [url], None, None
    size, mirrors = listing
    racers = [*mirrors[:_RACE_MIRRORS], _archive_url(url)]
    won = _race(racers, size, deadline)
    if won is None:
        return [url], size, None
    ranked, data, rate = won
    partial.write(data)
    # Untried mirrors before racers that were too slow to finish the race.
    spare = [m for m in mirrors[: _RACE_MIRRORS + _RACE_FALLBACKS] if m not in racers]
    late = [r for r in racers if r not in ranked]
    return [*ranked, *spare, *late, url], size, rate * FLOOR_FRACTION


def mirror_rate(url: str, checksum: str) -> tuple[float, int] | None:
    """The best racing mirror's rate and the file's size, for judging another download."""
    listing = _metalink(url, checksum)
    if listing is None:
        return None
    size, mirrors = listing
    won = _race([*mirrors[:_RACE_MIRRORS], _archive_url(url)], size, time.monotonic() + 30)
    return None if won is None else (won[2], size)


def download(url: str, target: Path, checksum: str) -> None:
    """Fetch verified bytes under one time budget, resuming across sources.

    ``_MAX_ATTEMPTS`` failed attempts (network errors, bad status, a wrong SHA-256) end it;
    a source that turns slow is left for the next without counting as one
    (``_MAX_SWITCHES``).
    """
    started = time.monotonic()
    deadline = started + DOWNLOAD_BUDGET_S
    try:
        with target.open("wb") as output:
            partial = _Partial(output)
            sources, size, floor = _sources(url, checksum, partial, deadline)
            failures = switches = index = 0
            used: list[str] = []
            while True:
                if time.monotonic() >= deadline:
                    raise DocumentError(
                        f"LibreOffice download exceeded {DOWNLOAD_BUDGET_S:.0f} seconds"
                    )
                source = sources[index % len(sources)]
                try:
                    served = _stream(source, partial, size, floor, deadline, source != url)
                    used.append(_host(served))
                    actual = partial.digest.hexdigest()
                    if actual != checksum:
                        count = partial.count
                        partial.restart()
                        raise _RetryableDownloadError(
                            f"LibreOffice download failed its pinned SHA-256 check: expected "
                            f"{checksum}, received {actual} for {count} bytes from {served}"
                        )
                except _SlowMirror as slow:
                    used.append(_host(source))
                    switches += 1
                    index += 1
                    logger.warning(
                        "reason=office_mirror_switch from=%s to=%s offset=%d mb_s=%.2f "
                        "floor_mb_s=%.2f",
                        _host(source),
                        _host(sources[index % len(sources)]),
                        partial.count,
                        slow.rate / 1e6,
                        (floor or 0.0) / 1e6,
                    )
                    if switches >= _MAX_SWITCHES or index >= len(sources) - 1:
                        floor = None  # ride out the last source rather than hop forever
                    continue
                except _RetryableDownloadError as exc:
                    failures += 1
                    if failures == _MAX_ATTEMPTS:
                        raise DocumentError(
                            f"{exc}; all {_MAX_ATTEMPTS} download attempts failed"
                        ) from exc
                    logger.warning(
                        "reason=office_download_retry attempt=%d url=%s error=%s",
                        failures,
                        source,
                        exc,
                    )
                    backoff = min(2.0 ** (failures - 1), _BACKOFF_CAP_S)
                    remaining = max(0.0, deadline - time.monotonic())
                    time.sleep(min(backoff * random.uniform(0.5, 1.5), remaining))
                    index += 1
                    continue
                elapsed = max(time.monotonic() - started, 1e-3)
                logger.info(
                    "reason=office_download_done mirror=%s bytes=%d seconds=%.1f mb_s=%.1f "
                    "sources=%s retries=%d switches=%d",
                    used[-1],
                    partial.count,
                    elapsed,
                    partial.count / elapsed / 1e6,
                    ",".join(dict.fromkeys(used)),
                    failures,
                    switches,
                )
                return
    except DocumentError:
        target.unlink(missing_ok=True)
        raise
