"""OAuth token durability across process restarts (#1285, C1-S5 item 5).

Before this slice, tools/mcp_runtime.py::_oauth_provider_from_config defaulted
to a process-local in-memory TokenStorage -- every restart forced a fresh
OAuth flow. These tests exercise DurableFileTokenStorage directly (a fresh
instance per "restart", pointed at the SAME file, simulating what actually
survives a process boundary) and the factory's default wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from clio_agent.tools.mcp_oauth_storage import DurableFileTokenStorage


@pytest.mark.asyncio
async def test_tokens_survive_a_simulated_restart(tmp_path: Path) -> None:
    path = tmp_path / "oauth.json"
    first = DurableFileTokenStorage("https://mcp.example.com", path=path)
    await first.set_tokens(OAuthToken(access_token="tok-1", token_type="Bearer"))

    # A fresh instance == what a process restart actually gets: no shared state,
    # only the file.
    second = DurableFileTokenStorage("https://mcp.example.com", path=path)
    tokens = await second.get_tokens()

    assert tokens is not None
    assert tokens.access_token == "tok-1"


@pytest.mark.asyncio
async def test_client_info_survives_a_simulated_restart(tmp_path: Path) -> None:
    path = tmp_path / "oauth.json"
    first = DurableFileTokenStorage("https://mcp.example.com", path=path)
    await first.set_client_info(
        OAuthClientInformationFull(client_id="cid-1", redirect_uris=["https://cb.example.com"])
    )

    second = DurableFileTokenStorage("https://mcp.example.com", path=path)
    info = await second.get_client_info()

    assert info is not None
    assert info.client_id == "cid-1"


@pytest.mark.asyncio
async def test_no_prior_entry_returns_none(tmp_path: Path) -> None:
    storage = DurableFileTokenStorage("https://never-authed.example.com", path=tmp_path / "oauth.json")
    assert await storage.get_tokens() is None
    assert await storage.get_client_info() is None


@pytest.mark.asyncio
async def test_different_server_urls_never_share_credentials(tmp_path: Path) -> None:
    """H8: credentials never reused across authorization servers."""

    path = tmp_path / "oauth.json"
    server_a = DurableFileTokenStorage("https://a.example.com", path=path)
    server_b = DurableFileTokenStorage("https://b.example.com", path=path)

    await server_a.set_tokens(OAuthToken(access_token="a-token", token_type="Bearer"))

    assert (await server_a.get_tokens()).access_token == "a-token"
    assert await server_b.get_tokens() is None


@pytest.mark.asyncio
async def test_malformed_entry_degrades_to_none_not_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "oauth.json"
    path.write_text(
        '{"schema": "clio-agent.mcp-oauth-tokens.v1", '
        '"entries": {"https://x.example.com": {"tokens": {"not": "a valid token shape"}}}}',
        encoding="utf-8",
    )
    storage = DurableFileTokenStorage("https://x.example.com", path=path)
    assert await storage.get_tokens() is None


@pytest.mark.asyncio
async def test_schema_mismatch_drops_entries_never_crashes(tmp_path: Path) -> None:
    path = tmp_path / "oauth.json"
    path.write_text('{"schema": "some-other-schema", "entries": {}}', encoding="utf-8")
    storage = DurableFileTokenStorage("https://x.example.com", path=path)
    assert await storage.get_tokens() is None


@pytest.mark.asyncio
async def test_unwritable_directory_degrades_without_raising(tmp_path: Path, monkeypatch) -> None:
    """A durability failure must never break the live OAuth flow itself."""

    storage = DurableFileTokenStorage("https://x.example.com", path=tmp_path / "sub" / "oauth.json")

    def _boom(*_args, **_kwargs):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(Path, "mkdir", _boom)
    await storage.set_tokens(OAuthToken(access_token="tok", token_type="Bearer"))  # must not raise


def test_factory_defaults_to_durable_storage() -> None:
    from clio_agent.tools.mcp_config import MCPAuthConfig
    from clio_agent.tools.mcp_runtime import _oauth_provider_from_config

    config = MCPAuthConfig(client_metadata={"redirect_uris": ["https://cb.example.com"]})
    provider = _oauth_provider_from_config("https://mcp.example.com", config)

    assert provider is not None
    assert isinstance(provider.context.storage, DurableFileTokenStorage)


def test_factory_honors_explicit_storage_override() -> None:
    from clio_agent.tools.mcp_config import MCPAuthConfig
    from clio_agent.tools.mcp_runtime import _oauth_provider_from_config

    class _Explicit:
        async def get_tokens(self):
            return None

        async def set_tokens(self, tokens):
            pass

        async def get_client_info(self):
            return None

        async def set_client_info(self, client_info):
            pass

    explicit = _Explicit()
    config = MCPAuthConfig(
        client_metadata={"redirect_uris": ["https://cb.example.com"]}, storage=explicit
    )
    provider = _oauth_provider_from_config("https://mcp.example.com", config)

    assert provider is not None
    assert provider.context.storage is explicit
