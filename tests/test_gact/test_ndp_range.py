"""An attached NDP resource is read by byte range, on the same route as a whole read."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from clio_agent.gact.storage.ndp_catalog import NDPDataset, NDPEndpoint, NDPResource
from clio_agent.gact.storage.ndp_source import NDPSource
from clio_agent.gact.storage.range_handles import RANGE_PROVIDERS, RANGE_UNSUPPORTED

BODY = bytes(range(256)) * 64  # 16 KiB


def _source(url: str, size: int = len(BODY)) -> NDPSource:
    return NDPSource(
        NDPDataset(
            id="d",
            name="d",
            title="Dataset",
            endpoint=NDPEndpoint(
                id="ep", name="Endpoint", url="https://endpoint.test/api", authenticated=True
            ),
            resources=[NDPResource(id="r", path="tile.tif", name="tile.tif", url=url, size=size)],
        ),
        lambda: "private-access",
    )


def _serve(monkeypatch: pytest.MonkeyPatch, respond: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    original = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    return seen


def _ranged(request: httpx.Request) -> httpx.Response:
    first, last = request.headers["Range"].removeprefix("bytes=").split("-")
    start, end = int(first), int(last)
    return httpx.Response(
        206,
        content=BODY[start : end + 1],
        headers={"Content-Range": f"bytes {start}-{end}/{len(BODY)}"},
    )


def test_ndp_is_a_range_provider() -> None:
    assert "ndp" in RANGE_PROVIDERS


def test_a_range_follows_the_signed_redirect_without_the_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "endpoint.test":
            assert request.headers["Authorization"] == "Bearer private-access"
            return httpx.Response(302, headers={"Location": "https://data.test/o?sig=a%2Bb"})
        assert "Authorization" not in request.headers
        assert request.url.query == b"sig=a%2Bb"
        return _ranged(request)

    seen = _serve(monkeypatch, respond)
    source = _source("pelican://osg-htc.org/science/tile.tif")
    reader = source.range_reader(source.entries()[0])
    assert reader.read_range(1000, 50) == BODY[1000:1050]
    assert all(r.headers["Range"] == "bytes=1000-1049" for r in seen)  # on every hop
    assert reader.read_range(len(BODY) - 10, 100) == BODY[-10:]  # clipped at the end
    assert reader.requests == 2 and reader.bytes_read == 60


def test_a_missing_catalog_size_is_asked_of_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, _ranged)
    source = _source("https://data.test/tile.tif", size=0)
    assert source.range_reader(source.entries()[0]).total_size() == len(BODY)


def test_a_server_that_ignores_range_is_refused_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, lambda _r: httpx.Response(200, content=BODY))
    source = _source("https://data.test/tile.tif")
    reader = source.range_reader(source.entries()[0])
    with pytest.raises(ValueError, match=f"{RANGE_UNSUPPORTED}: .*HTTP 200"):
        reader.read_range(0, 10)
    with pytest.raises(ValueError, match=RANGE_UNSUPPORTED):
        _source("https://data.test/tile.tif", size=0).range_reader(source.entries()[0]).total_size()


def test_a_whole_read_still_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, lambda _r: httpx.Response(200, content=BODY))
    source = _source("https://data.test/tile.tif")
    with source.open_read(source.entries()[0]) as stream:
        assert stream.read() == BODY
    assert "Range" not in seen[0].headers


def test_later_ranges_reuse_the_resolved_address(monkeypatch: pytest.MonkeyPatch) -> None:
    signed = {"n": 0}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.host == "endpoint.test":
            signed["n"] += 1
            return httpx.Response(
                302, headers={"Location": f"https://data.test/o?sig={signed['n']}"}
            )
        assert "Authorization" not in request.headers
        if request.url.query != f"sig={signed['n']}".encode():  # an older signature expired
            return httpx.Response(403)
        return _ranged(request)

    seen = _serve(monkeypatch, respond)
    source = _source("pelican://osg-htc.org/science/tile.tif")
    reader = source.range_reader(source.entries()[0])
    for start in range(0, 16 * 1024, 1024):
        assert reader.read_range(start, 1024) == BODY[start : start + 1024]
    assert reader.resolved == 1 and sum(r.url.host == "endpoint.test" for r in seen) == 1
    signed["n"] += 1  # the Endpoint signs anew: the kept address now answers 403
    assert reader.read_range(10, 5) == BODY[10:15]
    assert reader.resolved == 2
