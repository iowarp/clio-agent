"""Configuration readiness probes accept the same declarative transport shapes."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.routes import mcp_configuration


@pytest.mark.parametrize(
    ("spec", "kind"),
    [
        ({"command": "web-mcp", "args": []}, "stdio"),
        ({"url": "https://example.org/mcp"}, "http"),
        ({"type": "sse", "url": "https://example.org/sse"}, "sse"),
        ({"transport": "streamable-http", "url": "https://example.org/mcp"}, "streamable-http"),
    ],
)
def test_probe_normalizes_declarations_without_rewriting_saved_config(
    spec: dict[str, Any], kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing raw-spec transport is a declaration default, not an invalid server."""
    seen: list[dict[str, Any]] = []
    original = dict(spec)

    class Client:
        async def __aenter__(self) -> "Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def list_tools(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(name="search"), SimpleNamespace(name="fetch")]

    def transport(value: dict[str, Any]) -> object:
        seen.append(value)
        return object()

    monkeypatch.setattr(mcp_configuration, "transport_from_spec", transport)
    monkeypatch.setattr(mcp_configuration, "make_mcp_client", lambda *args, **kwargs: Client())
    assert asyncio.run(mcp_configuration._probe_user_mcp_server(spec)) == (
        ["search", "fetch"],
        None,
    )
    assert seen == [{**spec, "transport": kind}]
    assert spec == original


def test_probe_keeps_explicit_invalid_transport_as_a_visible_failure() -> None:
    """Normalization must not silently replace an explicitly invalid transport."""
    tools, error = asyncio.run(
        mcp_configuration._probe_user_mcp_server({"transport": "invalid", "command": "web-mcp"})
    )
    assert tools == []
    assert error is not None and "unknown MCP transport 'invalid'" in error
