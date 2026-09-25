"""Durable ChatGPT OAuth credential store (A.4).

One credential per machine (mirrors the previous single ``~/.codex/auth.json``
model, except CLIO owns this file end to end and never reads or writes
``~/.codex/auth.json``). Persistence reuses
:class:`clio_agent.tools.atomic_json_store.AtomicJsonFileStore` (0600, atomic
write-before-use) rather than duplicating that dance.

Refresh is proactive (under :data:`~clio_agent.providers.chatgpt.constants.REFRESH_MARGIN_MS`
remaining) and on a 401, guarded by a lock so concurrent sessions never race a
refresh_token exchange — refresh tokens rotate, so a lost write means the user
has to sign in again. Never log tokens: only the typed reason strings below
ever reach a log line.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from clio_agent import paths
from clio_agent.providers.chatgpt import constants as c
from clio_agent.providers.chatgpt.errors import (
    ChatGPTCredentialMissingError,
    ChatGPTRefreshFailedError,
)
from clio_agent.providers.chatgpt.login_flow import ChatGptCredential
from clio_agent.providers.chatgpt.oauth import (
    OAuthError,
    decode_account_id,
)
from clio_agent.providers.chatgpt.oauth import (
    refresh_token as _refresh_token_request,
)
from clio_agent.tools.atomic_json_store import AtomicJsonFileStore

__all__ = ["ChatGptCredentialStore"]

_SCHEMA = "clio-agent.chatgpt-credential.v1"
_FILE_BASENAME = "chatgpt_credential.json"
_ENTRY_KEY = "default"


def _default_path() -> Path:
    return paths.user_config_dir() / _FILE_BASENAME


class ChatGptCredentialStore:
    """Load, persist, refresh, and delete the machine's ChatGPT credential."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._store = AtomicJsonFileStore(
            path or _default_path(), schema=_SCHEMA, trace_tag="PROVIDERS"
        )
        self._refresh_lock = threading.Lock()

    def load(self) -> ChatGptCredential | None:
        """Return the stored credential, or ``None`` when never signed in / logged out."""

        raw = self._store.read_entries().get(_ENTRY_KEY)
        if not isinstance(raw, dict):
            return None
        credential = ChatGptCredential.from_dict(raw)
        if not credential.access_token or not credential.refresh_token or not credential.account_id:
            return None
        return credential

    def save(self, credential: ChatGptCredential) -> None:
        """Persist ``credential``, atomically replacing any previous one."""

        entries = self._store.read_entries()
        entries[_ENTRY_KEY] = credential.to_dict()
        self._store.write_entries(entries)

    def logout(self) -> None:
        """Delete the stored credential (A.4). A no-op when already signed out."""

        entries = self._store.read_entries()
        if entries.pop(_ENTRY_KEY, None) is not None:
            self._store.write_entries(entries)

    def is_signed_in(self) -> bool:
        return self.load() is not None

    def _needs_refresh(self, credential: ChatGptCredential) -> bool:
        return credential.expires_at_ms - int(time.time() * 1000) < c.REFRESH_MARGIN_MS

    def get_valid_credential(self, *, force_refresh: bool = False) -> ChatGptCredential:
        """Return a credential with a live access token, refreshing proactively.

        Args:
            force_refresh: Refresh unconditionally (used after a 401).

        Raises:
            ChatGPTCredentialMissingError: no stored credential.
            ChatGPTRefreshFailedError: the stored credential needed a refresh
                and the refresh request itself was rejected -- the caller
                should mark the credential invalid and ask the user to sign
                in again (A.4/A.7).
        """

        credential = self.load()
        if credential is None:
            raise ChatGPTCredentialMissingError("No stored ChatGPT credential. Sign in first.")
        if not force_refresh and not self._needs_refresh(credential):
            return credential
        return self._refresh(credential, force=force_refresh)

    def _refresh(self, credential: ChatGptCredential, *, force: bool = False) -> ChatGptCredential:
        """Refresh under a lock; a racing caller sees the winner's fresh token, not a second exchange.

        ``force`` skips the "someone already refreshed while we waited for the
        lock" short-circuit -- a 401 means the access token was rejected NOW,
        which is independent of ``expires_at_ms`` (a revoked-but-not-yet-expired
        credential), so :meth:`mark_invalid_after_401` must always exchange the
        refresh token at least once, never silently hand back the same stale
        access token.
        """

        with self._refresh_lock:
            # Re-check after acquiring the lock: another thread may already have
            # refreshed (and persisted a new refresh_token) while this one waited.
            current = self.load() or credential
            if not force and not self._needs_refresh(current):
                return current
            try:
                tokens = _refresh_token_request(current.refresh_token)
                account_id = decode_account_id(tokens.access_token)
            except OAuthError as exc:
                raise ChatGPTRefreshFailedError(
                    f"ChatGPT token refresh failed: {exc}. Sign in again."
                ) from exc
            refreshed = ChatGptCredential(
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
                expires_at_ms=int(time.time() * 1000) + tokens.expires_in * 1000,
                account_id=account_id,
            )
            # Write the new access/refresh pair atomically BEFORE it is used
            # anywhere else -- refresh tokens rotate, so a lost write here
            # means the user has to log in again (A.4).
            self.save(refreshed)
            return refreshed

    def mark_invalid_after_401(self) -> ChatGptCredential:
        """Force a refresh after the backend rejected the current access token with a 401."""

        return self.get_valid_credential(force_refresh=True)
