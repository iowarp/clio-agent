"""Transient download failures retry pinned bytes, with one bounded budget."""

from __future__ import annotations

import hashlib
import io
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime.document_stack import office_fetch
from clio_agent.runtime.document_stack.process import DocumentError


@pytest.mark.parametrize("failure", ["checksum", "network", "http503"])
def test_fresh_request_recovers_without_accepting_unverified_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    payload = b"verified Office package"
    attempts: list[str] = []
    delays: list[float] = []

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        assert request.get_header("Cache-control") == "no-cache"
        if len(attempts) == 1:
            if failure == "network":
                raise urllib.error.URLError("connection closed")
            if failure == "http503":
                raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
            return io.BytesIO(b"unverified")
        return io.BytesIO(payload)

    monkeypatch.setattr(office_fetch.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(office_fetch.time, "sleep", delays.append)
    monkeypatch.setattr(office_fetch.random, "uniform", lambda low, high: 1.0)
    target = tmp_path / "package.part"
    office_fetch.download(
        "https://example.invalid/package", target, hashlib.sha256(payload).hexdigest()
    )
    assert target.read_bytes() == payload
    assert attempts == ["https://example.invalid/package"] * 2
    assert delays == [1.0]


def test_repeated_corrupt_download_fails_closed_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []
    delays: list[float] = []

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        return io.BytesIO(b"corrupt")

    monkeypatch.setattr(office_fetch.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(office_fetch.time, "sleep", delays.append)
    target = tmp_path / "package.part"
    limit = office_fetch._MAX_ATTEMPTS
    with pytest.raises(DocumentError, match=f"all {limit} download attempts failed") as failure:
        office_fetch.download("https://example.invalid/package", target, "0" * 64)
    assert len(attempts) == limit
    # Doubling backoff from 1 s, capped, with +/-50% jitter between attempts.
    assert len(delays) == limit - 1
    for failed, delay in enumerate(delays, start=1):
        backoff = min(2.0 ** (failed - 1), office_fetch._BACKOFF_CAP_S)
        assert 0.5 * backoff <= delay <= 1.5 * backoff
    assert not target.exists()
    assert "expected " + "0" * 64 in str(failure.value)
    assert "7 bytes" in str(failure.value)
    assert hashlib.sha256(b"corrupt").hexdigest() in str(failure.value)


def test_retry_does_not_reset_download_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 0.0}
    attempts: list[str] = []

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        return io.BytesIO(b"corrupt")

    def delay(seconds: float) -> None:
        clock["now"] = 601.0

    monkeypatch.setattr(office_fetch.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(office_fetch.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(office_fetch.time, "sleep", delay)
    target = tmp_path / "package.part"
    with pytest.raises(DocumentError, match="exceeded 600 seconds"):
        office_fetch.download("https://example.invalid/package", target, "0" * 64)
    assert len(attempts) == 1
    assert not target.exists()


def test_oversized_response_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        return io.BytesIO(b"oversized")

    monkeypatch.setattr(office_fetch.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(office_fetch, "MAX_DOWNLOAD", 3)
    target = tmp_path / "package.part"
    with pytest.raises(DocumentError, match="size limit"):
        office_fetch.download("https://example.invalid/package", target, "0" * 64)
    assert len(attempts) == 1
    assert not target.exists()


def test_immutable_package_not_found_does_not_retry_or_change_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    monkeypatch.setattr(office_fetch.urllib.request, "urlopen", fetch)
    with pytest.raises(DocumentError, match="HTTP 404"):
        office_fetch.download("https://example.invalid/immutable", tmp_path / "part", "0" * 64)
    assert attempts == ["https://example.invalid/immutable"]


def test_retired_tdf_asset_uses_official_archive_with_same_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []
    payload = b"verified archive bytes"
    url = "https://download.documentfoundation.org/libreoffice/stable/26.2.6/win/file.msi"

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        if len(attempts) <= 2:
            raise urllib.error.HTTPError(request.full_url, 404, "retired", {}, None)
        return io.BytesIO(payload)

    monkeypatch.setattr(office_fetch.urllib.request, "urlopen", fetch)
    target = tmp_path / "package.part"
    office_fetch.download(url, target, hashlib.sha256(payload).hexdigest())
    assert target.read_bytes() == payload
    assert attempts == [
        url + ".meta4",
        url,
        "https://downloadarchive.documentfoundation.org/libreoffice/old/26.2.6.2/win/file.msi",
    ]


def test_unreachable_librarian_pin_falls_back_to_its_snapshot_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []
    payload = b"verified userland package"
    url = "https://launchpadlibrarian.net/1/liblzma5_5.2.5-2ubuntu1.1_amd64.deb"
    path = "pool/main/x/xz-utils/liblzma5_5.2.5-2ubuntu1.1_amd64.deb"
    snapshot = f"https://snapshot.ubuntu.com/ubuntu/20261020T000000Z/{path}"

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        if request.full_url.startswith("https://archive.ubuntu.com/"):
            raise urllib.error.HTTPError(request.full_url, 404, "superseded", {}, None)
        if request.full_url != snapshot:
            raise urllib.error.URLError("handshake timed out")
        return io.BytesIO(payload)

    monkeypatch.setattr(office_fetch.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(office_fetch.time, "sleep", lambda seconds: None)
    target = tmp_path / "package.part"
    office_fetch.download(url, target, hashlib.sha256(payload).hexdigest(), (snapshot,))
    assert target.read_bytes() == payload
    assert attempts == [
        url,
        f"https://archive.ubuntu.com/ubuntu/{path}",
        "https://launchpad.net/ubuntu/+archive/primary/+files/" + path.rsplit("/", 1)[1],
        snapshot,
    ]


def test_superseded_ports_pin_falls_back_to_launchpad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []
    payload = b"verified arm64 userland package"
    name = "libnss3_3.98-0ubuntu0.22.04.5_arm64.deb"
    url = f"https://ports.ubuntu.com/ubuntu-ports/pool/main/n/nss/{name}"
    launchpad = f"https://launchpad.net/ubuntu/+archive/primary/+files/{name}"

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        if request.full_url == url:
            raise urllib.error.HTTPError(request.full_url, 404, "superseded", {}, None)
        return io.BytesIO(payload)

    monkeypatch.setattr(office_fetch.urllib.request, "urlopen", fetch)
    target = tmp_path / "package.part"
    office_fetch.download(url, target, hashlib.sha256(payload).hexdigest())
    assert target.read_bytes() == payload
    assert attempts == [url, launchpad]
