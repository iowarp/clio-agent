"""The bounded external-media fetch behind the ``download_media`` native tool.

Exercised fully in-memory: an ``httpx.MockTransport`` stands in for the
network and an injected resolver stands in for DNS, so every typed refusal
(scheme, private host, redirect target, HTTP status, size, content type,
sniffed-body mismatch) is proven without touching a real host.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import httpx
import pytest

from clio_agent.tools.media_download import (
    MediaDownloadError,
    MediaDownloadLimits,
    fetch_media,
    media_type_allowed,
    validate_media_url,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PUBLIC = "93.184.215.14"


def _limits(**overrides: object) -> MediaDownloadLimits:
    values: dict[str, object] = {
        "max_bytes": 1024,
        "allowed_media_types": ("image/*", "video/*", "audio/*"),
        "connect_timeout_s": 5.0,
        "read_timeout_s": 5.0,
        "allow_private_hosts": False,
    }
    values.update(overrides)
    return MediaDownloadLimits(**values)  # type: ignore[arg-type]


def _resolver(mapping: dict[str, list[str]] | None = None) -> Callable[[str, int], list[str]]:
    table = mapping or {}

    def resolve(host: str, _port: int) -> list[str]:
        return table.get(host, [PUBLIC])

    return resolve


def _transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _fetch(url: str, handler: Callable[[httpx.Request], httpx.Response], **kw: object):
    limits = kw.pop("limits", None) or _limits()
    resolver = kw.pop("resolver", None) or _resolver()
    return fetch_media(
        url,
        limits,  # type: ignore[arg-type]
        transport=_transport(handler),
        resolve=resolver,  # type: ignore[arg-type]
    )


def _reason(excinfo: pytest.ExceptionInfo[MediaDownloadError]) -> str:
    return excinfo.value.reason


# --------------------------------------------------------------------------- #
# Success
# --------------------------------------------------------------------------- #


def test_fetch_returns_bytes_media_type_and_hash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("clio-agent")
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    fetched = _fetch("https://images.example.org/cat.png", handler)

    assert fetched.body == PNG
    assert fetched.media_type == "image/png"
    assert fetched.sha256 == hashlib.sha256(PNG).hexdigest()
    assert fetched.final_url == "https://images.example.org/cat.png"
    assert fetched.host == "images.example.org"


def test_a_generic_declared_type_is_refined_from_the_bytes() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=JPEG, headers={"content-type": "application/octet-stream"}
        )

    fetched = _fetch("https://example.org/photo", handler)
    assert fetched.media_type == "image/jpeg"


def test_redirects_are_followed_and_each_hop_revalidated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://cdn.example.org/real.png"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    fetched = _fetch("https://example.org/start", handler)
    assert fetched.final_url == "https://cdn.example.org/real.png"


# --------------------------------------------------------------------------- #
# URL / host refusals
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.org/a.png",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "not a url",
        "https:///nohost.png",
        "https://user:secret@example.org/a.png",
    ],
)
def test_invalid_urls_are_typed_refusals(url: str) -> None:
    with pytest.raises(MediaDownloadError) as excinfo:
        validate_media_url(url, _limits(), resolve=_resolver())
    assert _reason(excinfo) == "media_download_invalid_url"
    assert excinfo.value.hint


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "fd00::1"]
)
def test_private_and_loopback_hosts_are_refused(address: str) -> None:
    resolver = _resolver({"internal.example": [address]})
    with pytest.raises(MediaDownloadError) as excinfo:
        validate_media_url("https://internal.example/a.png", _limits(), resolve=resolver)
    assert _reason(excinfo) == "media_download_host_not_allowed"


def test_a_host_resolving_to_any_private_address_is_refused() -> None:
    resolver = _resolver({"mixed.example": [PUBLIC, "10.1.2.3"]})
    with pytest.raises(MediaDownloadError) as excinfo:
        validate_media_url("https://mixed.example/a.png", _limits(), resolve=resolver)
    assert _reason(excinfo) == "media_download_host_not_allowed"


def test_allow_private_hosts_is_a_config_opt_in() -> None:
    resolver = _resolver({"lab.local": ["10.0.0.9"]})
    validate_media_url(
        "https://lab.local/a.png", _limits(allow_private_hosts=True), resolve=resolver
    )


def test_an_unresolvable_host_is_a_network_refusal() -> None:
    def resolve(_host: str, _port: int) -> list[str]:
        raise OSError("name or service not known")

    with pytest.raises(MediaDownloadError) as excinfo:
        validate_media_url("https://nope.invalid/a.png", _limits(), resolve=resolve)
    assert _reason(excinfo) == "media_download_network_error"


def test_a_redirect_into_a_private_network_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://metadata.internal/latest"})

    resolver = _resolver({"metadata.internal": ["169.254.169.254"]})
    with pytest.raises(MediaDownloadError) as excinfo:
        _fetch("https://example.org/a.png", handler, resolver=resolver)
    assert _reason(excinfo) == "media_download_host_not_allowed"


def test_a_redirect_loop_is_a_typed_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": str(request.url)})

    with pytest.raises(MediaDownloadError) as excinfo:
        _fetch("https://example.org/loop.png", handler)
    assert _reason(excinfo) == "media_download_too_many_redirects"


# --------------------------------------------------------------------------- #
# Response refusals
# --------------------------------------------------------------------------- #


def test_an_http_error_status_is_typed_with_the_status() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"missing")

    with pytest.raises(MediaDownloadError) as excinfo:
        _fetch("https://example.org/gone.png", handler)
    assert _reason(excinfo) == "media_download_http_error"
    assert "404" in excinfo.value.detail


def test_a_transport_failure_is_a_network_refusal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(MediaDownloadError) as excinfo:
        _fetch("https://example.org/a.png", handler)
    assert _reason(excinfo) == "media_download_network_error"


def test_a_declared_length_over_the_limit_is_refused_before_the_body() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=PNG,
            headers={"content-type": "image/png", "content-length": "999999"},
        )

    with pytest.raises(MediaDownloadError) as excinfo:
        _fetch("https://example.org/big.png", handler)
    assert _reason(excinfo) == "media_download_too_large"
    assert excinfo.value.details["max_bytes"] == 1024


def test_a_streamed_body_over_the_limit_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = PNG + b"\x00" * 4096

        def chunks():
            yield body

        return httpx.Response(200, content=chunks(), headers={"content-type": "image/png"})

    with pytest.raises(MediaDownloadError) as excinfo:
        _fetch("https://example.org/big.png", handler)
    assert _reason(excinfo) == "media_download_too_large"


@pytest.mark.parametrize("content_type", ["text/html", "application/pdf", "application/json"])
def test_a_non_media_content_type_is_refused(content_type: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html></html>", headers={"content-type": content_type})

    with pytest.raises(MediaDownloadError) as excinfo:
        _fetch("https://example.org/page", handler)
    assert _reason(excinfo) == "media_download_content_type_not_allowed"
    assert content_type in excinfo.value.detail


def test_a_body_that_is_not_the_declared_media_is_refused() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<!doctype html><p>login</p>", headers={"content-type": "image/png"}
        )

    with pytest.raises(MediaDownloadError) as excinfo:
        _fetch("https://example.org/fake.png", handler)
    assert _reason(excinfo) == "media_download_content_mismatch"


def test_the_allowed_types_come_from_the_limits() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    with pytest.raises(MediaDownloadError) as excinfo:
        _fetch(
            "https://example.org/a.png", handler, limits=_limits(allowed_media_types=("audio/*",))
        )
    assert _reason(excinfo) == "media_download_content_type_not_allowed"


def test_media_type_patterns() -> None:
    assert media_type_allowed("image/png", ("image/*",))
    assert media_type_allowed("IMAGE/PNG", ("image/png",))
    assert not media_type_allowed("image/png", ("video/*",))
    assert not media_type_allowed("", ("image/*",))


def test_error_result_shape_is_typed() -> None:
    error = MediaDownloadError("media_download_too_large", "too big", hint="use a smaller file")
    assert error.to_result() == {
        "ok": False,
        "reason": "media_download_too_large",
        "detail": "too big",
        "hint": "use a smaller file",
    }


def test_limits_resolve_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_MEDIA_DOWNLOAD_MAX_BYTES", "2MiB")
    monkeypatch.setenv("CLIO_MEDIA_DOWNLOAD_MEDIA_TYPES", "image/png,audio/*")
    limits = MediaDownloadLimits.from_config()
    assert limits.max_bytes == 2 * 1024 * 1024
    assert limits.allowed_media_types == ("image/png", "audio/*")
