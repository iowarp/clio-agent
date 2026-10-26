"""Configuration readiness probes accept the same declarative transport shapes."""

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from clio_agent.gact.routes import mcp_configuration
from clio_agent.tools.mcp_config import MCPServerSpec


def test_probe_detects_sign_in_on_post_only_mcp_without_registering_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 405 on GET must not hide a POST-only endpoint's authentication challenge."""
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(405 if request.method == "GET" else 401)

    class FailedClient:
        async def __aenter__(self) -> None:
            raise RuntimeError("SDK error without HTTP headers")

        async def __aexit__(self, *args: object) -> None:
            return None

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        mcp_configuration, "make_mcp_client", lambda *args, **kwargs: FailedClient()
    )
    monkeypatch.setattr(
        mcp_configuration.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    tools, error = asyncio.run(
        mcp_configuration._probe_user_mcp_server(
            {"transport": "http", "url": "https://example.test/mcp"}
        )
    )
    assert tools == []
    assert error and error.startswith("mcp_auth_required:")
    assert [request.method for request in requests] == ["GET", "POST"]
    assert b'"method":"ping"' in requests[1].content


@pytest.mark.parametrize(
    ("spec", "kind"),
    [
        ({"command": "web-mcp", "args": []}, "stdio"),
        ({"url": "https://example.org/mcp"}, "http"),
        ({"type": "sse", "url": "https://example.org/sse"}, "sse"),
        ({"transport": "streamable-http", "url": "https://example.org/mcp"}, "streamable-http"),
        ({"transport": "http", "url": "https://example.org/mcp", "always_load": False}, "http"),
    ],
)
def test_probe_normalizes_declarations_without_rewriting_saved_config(
    spec: dict[str, Any], kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing raw-spec transport is a declaration default, not an invalid server."""
    seen: list[MCPServerSpec] = []
    original = dict(spec)

    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def list_tools(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(name="search"), SimpleNamespace(name="fetch")]

    def transport(value: MCPServerSpec) -> object:
        seen.append(value)
        return object()

    monkeypatch.setattr(mcp_configuration, "transport_for", transport)
    monkeypatch.setattr(mcp_configuration, "make_mcp_client", lambda *args, **kwargs: Client())
    assert asyncio.run(mcp_configuration._probe_user_mcp_server(spec)) == (
        ["search", "fetch"],
        None,
    )
    assert seen[0].transport == ("http" if kind in {"sse", "streamable-http"} else kind)
    assert seen[0].legacy_sse == (kind == "sse")
    assert seen[0].always_load is False
    assert spec == original


def test_probe_keeps_explicit_invalid_transport_as_a_visible_failure() -> None:
    """Normalization must not silently replace an explicitly invalid transport."""
    tools, error = asyncio.run(
        mcp_configuration._probe_user_mcp_server({"transport": "invalid", "command": "web-mcp"})
    )
    assert tools == []
    assert error is not None and "transport" in error and "invalid" in error
