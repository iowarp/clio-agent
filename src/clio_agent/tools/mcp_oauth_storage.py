"""Durable OAuth token storage across process restarts (#1285, C1-S5 item 5).

Before this module, ``tools/mcp_runtime.py::_oauth_provider_from_config``
defaulted to a process-local in-memory ``TokenStorage`` when a caller
supplied no ``config.storage`` -- every restart forced a fresh OAuth flow
even against a server the user already authorized.
:class:`DurableFileTokenStorage` persists the SDK's own ``TokenStorage``
protocol (``get_tokens``/``set_tokens``/``get_client_info``/
``set_client_info``) to a single JSON file under the user's config dir,
keyed by server URL -- the only identity the SDK's
``OAuthClientProvider(server_url=...)`` construction site has available
(H8's ideal is per-AS-issuer keying; issuer resolution happens later, during
metadata discovery, than where a ``TokenStorage`` is constructed today, so
server-URL keying is the closest available proxy, not literal issuer
binding). ``_oauth_provider_from_config`` now builds one of these as its
DEFAULT (an explicit ``MCPAuthConfig(storage=...)`` always wins).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from clio_agent import paths
from clio_agent.runtime import trace

if TYPE_CHECKING:
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

__all__ = ["DurableFileTokenStorage"]

_SCHEMA = "clio-agent.mcp-oauth-tokens.v1"
_FILE_BASENAME = "mcp_oauth_tokens.json"
_lock = threading.Lock()


def _default_path() -> Path:
    return paths.user_config_dir() / _FILE_BASENAME


class DurableFileTokenStorage:
    """Disk-persisted ``mcp.client.auth.oauth2.TokenStorage`` for one server URL.

    Args:
        server_url: The MCP server URL this instance's tokens/client-info are
            scoped to -- never shared across a different ``server_url`` (H8:
            credentials never reused across authorization servers).
        path: Override for the backing JSON file (tests only; defaults to
            ``paths.user_config_dir() / "mcp_oauth_tokens.json"``).
    """

    def __init__(self, server_url: str, *, path: Path | None = None) -> None:
        self._server_url = server_url
        self._path = path or _default_path()

    def _load_entries(self) -> dict[str, dict[str, object]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            trace.event("TOOLS", "mcp_oauth_storage_unreadable path=%s reason=%s", self._path, exc)
            return {}
        if raw.get("schema") != _SCHEMA:
            trace.event(
                "TOOLS", "mcp_oauth_storage_schema_mismatch got=%s want=%s", raw.get("schema"), _SCHEMA
            )
            return {}
        entries = raw.get("entries")
        return entries if isinstance(entries, dict) else {}

    def _save_entries(self, entries: dict[str, dict[str, object]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"schema": _SCHEMA, "entries": entries}, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)
        # Best-effort OS file-permission restriction (POSIX only -- os.chmod on
        # Windows cannot express owner-only ACLs; the user profile directory's
        # own ACL is Windows's access boundary for this file, same posture as
        # every other clio_agent user-config file. Never fatal either way.
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass

    async def get_tokens(self) -> "OAuthToken | None":
        """Return the persisted token bundle, or ``None`` on a miss/unreadable entry."""

        from mcp.shared.auth import OAuthToken

        with _lock:
            entry = self._load_entries().get(self._server_url)
        raw = entry.get("tokens") if entry else None
        if raw is None:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - malformed entry degrades to a fresh OAuth flow
            trace.event(
                "TOOLS", "mcp_oauth_storage_tokens_undecodable server=%s reason=%s", self._server_url, exc
            )
            return None

    async def set_tokens(self, tokens: "OAuthToken") -> None:
        """Persist the token bundle for this instance's server URL."""

        try:
            with _lock:
                entries = self._load_entries()
                entry = dict(entries.get(self._server_url) or {})
                entry["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
                entries[self._server_url] = entry
                self._save_entries(entries)
        except Exception as exc:  # noqa: BLE001 - a durability failure must never break the OAuth
            # flow itself; the token still lives for THIS process (the caller's live
            # OAuthClientProvider state), just not across a restart.
            trace.event(
                "TOOLS", "mcp_oauth_storage_tokens_store_failed server=%s reason=%s", self._server_url, exc
            )

    async def get_client_info(self) -> "OAuthClientInformationFull | None":
        """Return the persisted dynamically-registered client info, or ``None``."""

        from mcp.shared.auth import OAuthClientInformationFull

        with _lock:
            entry = self._load_entries().get(self._server_url)
        raw = entry.get("client_info") if entry else None
        if raw is None:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - malformed entry degrades to re-registration
            trace.event(
                "TOOLS",
                "mcp_oauth_storage_client_info_undecodable server=%s reason=%s",
                self._server_url,
                exc,
            )
            return None

    async def set_client_info(self, client_info: "OAuthClientInformationFull") -> None:
        """Persist dynamically-registered client info for this instance's server URL."""

        try:
            with _lock:
                entries = self._load_entries()
                entry = dict(entries.get(self._server_url) or {})
                entry["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
                entries[self._server_url] = entry
                self._save_entries(entries)
        except Exception as exc:  # noqa: BLE001 - see set_tokens: durability-only failure
            trace.event(
                "TOOLS",
                "mcp_oauth_storage_client_info_store_failed server=%s reason=%s",
                self._server_url,
                exc,
            )
