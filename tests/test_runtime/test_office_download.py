"""Transient download failures retry pinned bytes, with one bounded budget."""

from __future__ import annotations

import hashlib
import io
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from clio_agent.runtime.document_stack import office
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

    monkeypatch.setattr(office.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(office.time, "sleep", delays.append)
    target = tmp_path / "package.part"
    office._download("https://example.invalid/package", target, hashlib.sha256(payload).hexdigest())
    assert target.read_bytes() == payload
    assert attempts == ["https://example.invalid/package"] * 2
    assert delays == [1]


def test_repeated_corrupt_download_fails_closed_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        return io.BytesIO(b"corrupt")

    monkeypatch.setattr(office.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(office.time, "sleep", lambda delay: None)
    target = tmp_path / "package.part"
    with pytest.raises(DocumentError, match="all three download attempts failed") as failure:
        office._download("https://example.invalid/package", target, "0" * 64)
    assert len(attempts) == 3
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

    monkeypatch.setattr(office.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(office.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(office.time, "sleep", delay)
    target = tmp_path / "package.part"
    with pytest.raises(DocumentError, match="exceeded 600 seconds"):
        office._download("https://example.invalid/package", target, "0" * 64)
    assert len(attempts) == 1
    assert not target.exists()


def test_oversized_response_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        return io.BytesIO(b"oversized")

    monkeypatch.setattr(office.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(office, "MAX_DOWNLOAD", 3)
    target = tmp_path / "package.part"
    with pytest.raises(DocumentError, match="size limit"):
        office._download("https://example.invalid/package", target, "0" * 64)
    assert len(attempts) == 1
    assert not target.exists()


def test_immutable_package_not_found_does_not_retry_or_change_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    monkeypatch.setattr(office.urllib.request, "urlopen", fetch)
    with pytest.raises(DocumentError, match="HTTP 404"):
        office._download("https://example.invalid/immutable", tmp_path / "part", "0" * 64)
    assert attempts == ["https://example.invalid/immutable"]


def test_retired_tdf_asset_uses_official_archive_with_same_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[str] = []
    payload = b"verified archive bytes"
    url = "https://download.documentfoundation.org/libreoffice/stable/26.2.6/win/file.msi"

    def fetch(request: urllib.request.Request, **kwargs: Any) -> io.BytesIO:
        attempts.append(request.full_url)
        if len(attempts) == 1:
            raise urllib.error.HTTPError(request.full_url, 404, "retired", {}, None)
        return io.BytesIO(payload)

    monkeypatch.setattr(office.urllib.request, "urlopen", fetch)
    target = tmp_path / "package.part"
    office._download(url, target, hashlib.sha256(payload).hexdigest())
    assert target.read_bytes() == payload
    assert attempts == [
        url,
        "https://downloadarchive.documentfoundation.org/libreoffice/old/26.2.6.2/win/file.msi",
    ]
