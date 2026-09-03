"""OAuth token durability across process restarts (#1285, C1-S5 item 5).

Before this slice, tools/mcp_runtime.py::_oauth_provider_from_config defaulted
to a process-local in-memory TokenStorage -- every restart forced a fresh
OAuth flow. These tests exercise DurableFileTokenStorage directly (a fresh
instance per "restart", pointed at the SAME file, simulating what actually
survives a process boundary) and the factory's default wiring.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any

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


@pytest.mark.asyncio
async def test_chmod_failure_emits_a_typed_reason_not_a_silent_swallow(
    tmp_path: Path, monkeypatch
) -> None:
    """#1285 review round SHOULD 5: os.chmod failing on the security-control
    re-assertion must reach the trace, typed -- never a bare `except OSError:
    pass``. The write itself must still succeed (durability is not the same
    control as the permission tightening)."""

    import os as os_mod

    import clio_agent.tools.mcp_oauth_storage as storage_mod

    events: list[tuple[Any, ...]] = []
    orig_event = storage_mod.trace.event

    def _spy(tag: str, fmt: str, *args: Any) -> None:
        events.append((tag, fmt, *args))
        orig_event(tag, fmt, *args)

    monkeypatch.setattr(storage_mod.trace, "event", _spy)

    real_chmod = os_mod.chmod

    def _boom(path, mode, *a, **k):
        if str(path).endswith("oauth.json"):
            raise OSError("simulated chmod failure")
        return real_chmod(path, mode, *a, **k)

    monkeypatch.setattr(storage_mod.os, "chmod", _boom)

    path = tmp_path / "oauth.json"
    storage = DurableFileTokenStorage("https://x.example.com", path=path)
    await storage.set_tokens(OAuthToken(access_token="tok", token_type="Bearer"))

    chmod_events = [e for e in events if e[1].startswith("mcp_oauth_storage_chmod_failed")]
    assert len(chmod_events) == 1, f"expected exactly one typed chmod-failure event, got {events!r}"
    assert path.exists(), "the write itself must still succeed despite the chmod failure"
    assert json.loads(path.read_text(encoding="utf-8"))["entries"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
@pytest.mark.asyncio
async def test_saved_file_is_0600_from_creation_no_world_readable_window(tmp_path: Path) -> None:
    """#1285 review round SHOULD 5: the tmp file must be created AT 0o600,
    not written world-readable then chmod-ed afterward."""

    path = tmp_path / "oauth.json"
    storage = DurableFileTokenStorage("https://x.example.com", path=path)
    await storage.set_tokens(OAuthToken(access_token="tok", token_type="Bearer"))

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


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
