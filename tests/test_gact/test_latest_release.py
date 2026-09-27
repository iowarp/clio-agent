"""GET /v1/system/latest-release: every path settles to a typed 200, never a hang.

The web version panel used to fetch a GitHub release asset directly from the
browser; GitHub sends no CORS headers on that response, so the fetch was
blocked and the panel sat on "Checking..." forever. The server now does the
fetch (httpx, no browser CORS concern) and always returns a typed result --
a real version, or a typed degradation -- so a client-side query settles.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.routes import latest_release


class _FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeAsyncClient:
    def __init__(
        self, response: "_FakeResponse | None" = None, error: Exception | None = None
    ) -> None:
        self._response = response
        self._error = error

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, url: str) -> _FakeResponse:
        if self._error is not None:
            raise self._error
        assert self._response is not None
        return self._response


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """The TTL cache is a process-wide singleton -- isolate each test from it."""
    latest_release._CACHE = latest_release._Cache()
    yield
    latest_release._CACHE = latest_release._Cache()


def _client(tmp_path: Path) -> TestClient:
    return TestClient(build_app(sessions_path=tmp_path / "s.json", agent=None))


def test_reports_the_manifest_version(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        latest_release.httpx,
        "AsyncClient",
        lambda **_kw: _FakeAsyncClient(_FakeResponse(200, {"version": "0.9.4.17"})),
    )

    body = _client(tmp_path).get("/v1/system/latest-release").json()

    assert body["version"] == "0.9.4.17"
    assert body["degradation"] is None


def test_settles_to_a_typed_degradation_instead_of_hanging_when_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        latest_release.httpx,
        "AsyncClient",
        lambda **_kw: _FakeAsyncClient(error=httpx.ConnectError("boom")),
    )

    body = _client(tmp_path).get("/v1/system/latest-release").json()

    assert body["version"] is None
    assert body["degradation"]["reason"] == "manifest_unreachable"


def test_settles_to_a_typed_degradation_on_a_server_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        latest_release.httpx,
        "AsyncClient",
        lambda **_kw: _FakeAsyncClient(_FakeResponse(503, {})),
    )

    body = _client(tmp_path).get("/v1/system/latest-release").json()

    assert body["version"] is None
    assert body["degradation"]["reason"] == "manifest_unreachable"


_LATEST = "https://github.com/iowarp/clio-agent/releases/latest/download/latest-lite.json"
_ASSET = "https://github.com/iowarp/clio-agent/releases/download/v0.9.4.19/latest-lite.json"
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _github(asset_status: int, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Serve GitHub's real shape: ``latest/download`` 302s to the tagged asset."""

    seen: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == _LATEST:
            return httpx.Response(302, headers={"Location": _ASSET})
        if asset_status == 200:
            return httpx.Response(200, json={"version": "0.9.4.19"})
        return httpx.Response(asset_status, text="Not Found")

    monkeypatch.setattr(
        latest_release.httpx,
        "AsyncClient",
        lambda **kw: _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(_handler), **kw),
    )
    monkeypatch.setenv("CLIO_RELEASE_MANIFEST_URL", _LATEST)
    return seen


def test_follows_githubs_latest_download_redirect_to_the_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _github(200, monkeypatch)

    body = _client(tmp_path).get("/v1/system/latest-release").json()

    assert seen == [_LATEST, _ASSET]
    assert body["version"] == "0.9.4.19"
    assert body["degradation"] is None


def test_a_release_whose_manifest_is_not_uploaded_yet_is_typed_as_still_publishing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _github(404, monkeypatch)

    body = _client(tmp_path).get("/v1/system/latest-release").json()

    assert body["version"] is None
    assert body["degradation"]["reason"] == "manifest_not_published"
    assert body["degradation"]["message"] == "The latest release is still being published."


def test_settles_to_a_typed_degradation_when_the_manifest_has_no_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        latest_release.httpx,
        "AsyncClient",
        lambda **_kw: _FakeAsyncClient(_FakeResponse(200, {"notes": "no version here"})),
    )

    body = _client(tmp_path).get("/v1/system/latest-release").json()

    assert body["version"] is None
    assert body["degradation"]["reason"] == "manifest_missing_version"


def test_caches_the_result_within_the_ttl_instead_of_refetching_every_poll(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = {"count": 0}

    def _make_client(**_kw: Any) -> _FakeAsyncClient:
        calls["count"] += 1
        return _FakeAsyncClient(_FakeResponse(200, {"version": "0.9.4.17"}))

    monkeypatch.setattr(latest_release.httpx, "AsyncClient", _make_client)
    client = _client(tmp_path)

    first = client.get("/v1/system/latest-release").json()
    second = client.get("/v1/system/latest-release").json()

    assert first == second
    assert calls["count"] == 1
