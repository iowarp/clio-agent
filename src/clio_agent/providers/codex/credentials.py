"""Durable Codex OAuth credential store (A.4).

One credential per machine, owned by CLIO end to end. CLIO never WRITES the Codex
CLI's ``$CODEX_HOME/auth.json`` (default ``~/.codex``); without a CLIO sign-in the
direct transport authenticates with that CLI login instead, read (and refreshed under
the CLI's own lock) by lm15 -- :func:`codex_cli_auth_path` is the one place the path
is resolved, so ``CODEX_HOME`` is honoured everywhere. Persistence reuses
:class:`clio_agent.tools.atomic_json_store.AtomicJsonFileStore` (0600, atomic
write-before-use) rather than duplicating that dance.

Refresh is proactive (under :data:`~clio_agent.providers.codex.constants.REFRESH_MARGIN_MS`
remaining) and on a 401, guarded by a lock so concurrent sessions never race a
refresh_token exchange — refresh tokens rotate, so a lost write means the user
has to sign in again. Never log tokens: only the typed reason strings below
ever reach a log line.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from clio_agent import paths
from clio_agent.providers.codex import constants as c
from clio_agent.providers.codex.errors import (
    CodexCredentialMissingError,
    CodexRefreshFailedError,
)
from clio_agent.providers.codex.login_flow import CodexCredential
from clio_agent.providers.codex.oauth import (
    OAuthError,
    decode_account_id,
)
from clio_agent.providers.codex.oauth import (
    refresh_token as _refresh_token_request,
)
from clio_agent.tools.atomic_json_store import AtomicJsonFileStore

__all__ = ["CodexCredentialStore"]

_SCHEMA = "clio-agent.codex-credential.v1"
_FILE_BASENAME = "codex_credential.json"
_ENTRY_KEY = "default"


def _default_path() -> Path:
    return paths.user_config_dir() / _FILE_BASENAME


class CodexCredentialStore:
    """Load, persist, refresh, and delete the machine's Codex credential."""

    def __init__(self, *, path: Path | None = None) -> None:
        self._store = AtomicJsonFileStore(
            path or _default_path(), schema=_SCHEMA, trace_tag="PROVIDERS"
        )
        self._refresh_lock = threading.Lock()

    def load(self) -> CodexCredential | None:
        """Return the stored credential, or ``None`` when never signed in / logged out."""

        raw = self._store.read_entries().get(_ENTRY_KEY)
        if not isinstance(raw, dict):
            return None
        credential = CodexCredential.from_dict(raw)
        if not credential.access_token or not credential.refresh_token or not credential.account_id:
            return None
        return credential

    def save(self, credential: CodexCredential) -> None:
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

    def _needs_refresh(self, credential: CodexCredential) -> bool:
        return credential.expires_at_ms - int(time.time() * 1000) < c.REFRESH_MARGIN_MS

    def get_valid_credential(self, *, force_refresh: bool = False) -> CodexCredential:
        """Return a credential with a live access token, refreshing proactively.

        Args:
            force_refresh: Refresh unconditionally (used after a 401).

        Raises:
            CodexCredentialMissingError: no stored credential.
            CodexRefreshFailedError: the stored credential needed a refresh
                and the refresh request itself was rejected -- the caller
                should mark the credential invalid and ask the user to sign
                in again (A.4/A.7).
        """

        credential = self.load()
        if credential is None:
            raise CodexCredentialMissingError("No stored Codex credential. Sign in first.")
        if not force_refresh and not self._needs_refresh(credential):
            return credential
        return self._refresh(credential, force=force_refresh)

    def _refresh(self, credential: CodexCredential, *, force: bool = False) -> CodexCredential:
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
                raise CodexRefreshFailedError(
                    f"Codex token refresh failed: {exc}. Sign in again."
                ) from exc
            refreshed = CodexCredential(
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

    def mark_invalid_after_401(self) -> CodexCredential:
        """Force a refresh after the backend rejected the current access token with a 401."""

        return self.get_valid_credential(force_refresh=True)


# --------------------------------------------------------------------------- #
# The direct transport's sign-in: CLIO's own, else the local Codex CLI login     #
# --------------------------------------------------------------------------- #
def codex_cli_auth_path() -> Path:
    """The Codex CLI's login file (``$CODEX_HOME/auth.json``, default ``~/.codex``)."""
    home = os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")
    return Path(home) / "auth.json"


def codex_cli_signed_in() -> bool:
    """Whether the local Codex CLI login carries an access token and an account id."""
    try:
        tokens = json.loads(codex_cli_auth_path().read_text(encoding="utf-8")).get("tokens") or {}
    except (OSError, ValueError):
        return False
    return bool(tokens.get("access_token") and tokens.get("account_id"))


def direct_signed_in(store: CodexCredentialStore | None = None) -> bool:
    """Whether Codex direct can authenticate: CLIO's own sign-in, else the Codex CLI login."""
    return (store or CodexCredentialStore()).is_signed_in() or codex_cli_signed_in()


def direct_auth_headers(store: CodexCredentialStore | None = None) -> dict[str, str]:
    """``Authorization`` and ``chatgpt-account-id`` for a Codex backend request.

    CLIO's own sign-in is refreshed here; the Codex CLI login is read and refreshed by
    lm15 (which writes it back under the CLI's own lock) -- CLIO never rotates the CLI's
    refresh token itself.

    Raises:
        CodexCredentialMissingError: neither sign-in exists.
    """
    store = store or CodexCredentialStore()
    if store.is_signed_in():
        credential = store.get_valid_credential()
        return {
            "Authorization": f"Bearer {credential.access_token}",
            "chatgpt-account-id": credential.account_id,
        }
    if not codex_cli_signed_in():
        raise CodexCredentialMissingError()
    from dspy.lm15 import Message, OpenAICodexLM, Request

    wire = OpenAICodexLM.from_codex_cli()
    headers = dict(
        wire.build_request(
            Request(model="gpt-5.5", messages=(Message.user("."),)), stream=True
        ).headers
    )
    return {
        k: v for k, v in headers.items() if k.lower() in {"authorization", "chatgpt-account-id"}
    }
