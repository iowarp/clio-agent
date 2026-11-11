"""The Windows mxc fence's network proxy exports ALL_PROXY=socks5h:// to confined children.

httpx builds a transport for every environment proxy when a client is constructed, so without
``socksio`` (the ``httpx[socks]`` extra) the web MCP failed every call with "Using SOCKS proxy,
but the 'socksio' package is not installed".
"""

import httpx
import pytest

FENCE_PROXY_ENV = {
    "ALL_PROXY": "socks5h://127.0.0.1:1",
    "HTTP_PROXY": "http://127.0.0.1:2",
    "HTTPS_PROXY": "http://127.0.0.1:2",
}


@pytest.fixture
def fence_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("NO_PROXY", "no_proxy", "all_proxy", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name, value in FENCE_PROXY_ENV.items():
        monkeypatch.setenv(name, value)


def test_sync_client_constructs_under_fence_proxy_env(fence_proxy_env: None) -> None:
    with httpx.Client():
        pass


async def test_async_client_constructs_under_fence_proxy_env(fence_proxy_env: None) -> None:
    async with httpx.AsyncClient():
        pass
