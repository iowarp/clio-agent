"""Durable OAuth token storage across process restarts (#1285, C1-S5 item 5).

Before this module, ``tools/mcp_runtime.py::_oauth_provider_from_config``
defaulted to a process-local in-memory ``TokenStorage`` when a caller
supplied no ``config.storage`` -- every restart forced a fresh OAuth flow
even against a server the user already authorized.
:class:`DurableFileTokenStorage` persists the SDK's own ``TokenStorage``
protocol (``get_tokens``/``set_tokens``/``get_client_info``/
``set_client_info``) to a single JSON file under the user's config dir,
keyed by server URL and, for saved connections, their name. Discovery happens
after storage construction; validated authorization-server metadata and absolute
token expiry are retained alongside the SDK credentials for subsequent clients.
The runtime verifies the persisted client issuer before using that metadata.
``_oauth_provider_from_config`` uses this storage by default; an explicit
``MCPAuthConfig(storage=...)`` always wins.
"""

from __future__ import annotations

import json
import os
import threading
import time
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
    """Disk-persisted SDK token storage for one server and optional saved connection.

    Args:
        server_url: The MCP server URL this instance's tokens/client-info are
            scoped to -- never shared across a different ``server_url`` (H8:
            credentials never reused across authorization servers).
        path: Override for the backing JSON file (tests only; defaults to
            ``paths.user_config_dir() / "mcp_oauth_tokens.json"``).
        connection_id: Saved connection name; isolates separate accounts at the same URL.
    """

    def __init__(
        self, server_url: str, *, path: Path | None = None, connection_id: str | None = None
    ) -> None:
        self._server_url = server_url
        self._entry_key = (
            json.dumps([server_url, connection_id], separators=(",", ":"))
            if connection_id
            else server_url
        )
        self._path = path or _default_path()
        with _lock:
            self._generation = (self._load_entries().get(self._entry_key) or {}).get(
                "generation", 0
            )

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
                "TOOLS",
                "mcp_oauth_storage_schema_mismatch got=%s want=%s",
                raw.get("schema"),
                _SCHEMA,
            )
            return {}
        entries = raw.get("entries")
        return entries if isinstance(entries, dict) else {}

    def _save_entries(self, entries: dict[str, dict[str, object]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        payload = json.dumps({"schema": _SCHEMA, "entries": entries}, indent=1)
        # #1285 review round (SHOULD 5): create the tmp file AT 0o600 from the
        # moment it exists -- writing via `tmp.write_text(...)` then chmod-ing
        # the FINAL path afterward left a real window where an OAuth token
        # bundle sat on disk at the default (umask-derived, typically
        # world-readable) mode between creation and the chmod call. `os.open`'s
        # mode has no group/other bits to mask, so it applies exactly
        # regardless of umask, and `os.replace` preserves the source file's
        # mode on POSIX, so no window opens at the final path either.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, self._path)
        # Best-effort OS file-permission restriction (POSIX only -- os.chmod on
        # Windows cannot express owner-only ACLs; the user profile directory's
        # own ACL is Windows's access boundary for this file, same posture as
        # every other clio_agent user-config file). The file is already 0o600
        # from creation above; this re-asserts it defensively (e.g. a
        # filesystem/rename implementation that does not preserve mode). A
        # failure here is a real security-control gap, never a silent swallow
        # (#1285 review round SHOULD 5): it reaches the trace, typed.
        try:
            os.chmod(self._path, 0o600)
        except OSError as exc:
            trace.event(
                "TOOLS",
                "mcp_oauth_storage_chmod_failed path=%s reason=%s",
                self._path,
                exc,
            )

    async def get_tokens(self) -> "OAuthToken | None":
        """Return the persisted token bundle, or ``None`` on a miss/unreadable entry."""

        from mcp.shared.auth import OAuthToken

        with _lock:
            entry = self._load_entries().get(self._entry_key)
        raw = entry.get("tokens") if entry else None
        if raw is None:
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 - malformed entry degrades to a fresh OAuth flow
            trace.event(
                "TOOLS",
                "mcp_oauth_storage_tokens_undecodable server=%s reason=%s",
                self._server_url,
                exc,
            )
            return None

    def has_tokens(self) -> bool:
        """Check durable authorization without starting discovery or browser interaction."""
        from mcp.shared.auth import OAuthToken

        with _lock:
            entry = self._load_entries().get(self._entry_key) or {}
        if entry.get("generation", 0) != self._generation:
            return False
        try:
            return bool(OAuthToken.model_validate(entry.get("tokens")).access_token)
        except (ValueError, TypeError):
            return False

    def clear(self, *, client_only: bool = False) -> None:
        """Forget this connection's credentials, preserving other server entries."""
        with _lock:
            entries = self._load_entries()
            if client_only:
                entry = dict(entries.get(self._entry_key) or {})
                entry.pop("client_info", None)
                entries[self._entry_key] = entry
            else:
                previous = entries.get(self._entry_key) or {}
                generation = previous.get("generation", 0)
                entries[self._entry_key] = {
                    "generation": int(generation) + 1 if isinstance(generation, int) else 1
                }
            self._save_entries(entries)

    def authorization_context(self) -> dict[str, object]:
        """Read the validated SDK metadata and absolute expiry needed after restart."""
        with _lock:
            entry = self._load_entries().get(self._entry_key) or {}
            if entry.get("generation", 0) != self._generation:
                return {}
            context = entry.get("authorization_context")
            return dict(context) if isinstance(context, dict) else {}

    def save_authorization_context(self, metadata: dict[str, object], issuer: str | None) -> None:
        """Persist SDK-validated discovery metadata without changing credential ownership."""
        with _lock:
            entries = self._load_entries()
            entry = dict(entries.get(self._entry_key) or {})
            if entry.get("generation", 0) != self._generation:
                return
            entry["authorization_context"] = {"metadata": metadata, "issuer": issuer}
            entries[self._entry_key] = entry
            self._save_entries(entries)

    def token_expiry(self) -> float | None:
        """Return the original expiry, never a new lifetime measured from process start."""
        with _lock:
            entry = self._load_entries().get(self._entry_key) or {}
            expiry = entry.get("token_expires_at")
            return float(expiry) if isinstance(expiry, (int, float)) else None

    async def set_tokens(self, tokens: "OAuthToken") -> None:
        """Persist the token bundle for this instance's server URL."""

        try:
            with _lock:
                entries = self._load_entries()
                entry = dict(entries.get(self._entry_key) or {})
                if entry.get("generation", 0) != self._generation:
                    return  # An in-flight refresh cannot restore credentials after sign-out.
                entry["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
                entry["token_expires_at"] = (
                    time.time() + tokens.expires_in if tokens.expires_in is not None else None
                )
                entries[self._entry_key] = entry
                self._save_entries(entries)
        except Exception as exc:  # noqa: BLE001 - a durability failure must never break the OAuth
            # flow itself; the token still lives for THIS process (the caller's live
            # OAuthClientProvider state), just not across a restart.
            trace.event(
                "TOOLS",
                "mcp_oauth_storage_tokens_store_failed server=%s reason=%s",
                self._server_url,
                exc,
            )

    async def get_client_info(self) -> "OAuthClientInformationFull | None":
        """Return the persisted dynamically-registered client info, or ``None``."""

        from mcp.shared.auth import OAuthClientInformationFull

        with _lock:
            entry = self._load_entries().get(self._entry_key)
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
                entry = dict(entries.get(self._entry_key) or {})
                if entry.get("generation", 0) != self._generation:
                    return
                entry["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
                entries[self._entry_key] = entry
                self._save_entries(entries)
        except Exception as exc:  # noqa: BLE001 - see set_tokens: durability-only failure
            trace.event(
                "TOOLS",
                "mcp_oauth_storage_client_info_store_failed server=%s reason=%s",
                self._server_url,
                exc,
            )
