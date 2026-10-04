"""Trusted storage sign-in. Credentials never enter source records or agent tool results."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from clio_agent.gact.storage.models import SourceRecord
from clio_agent.tools.atomic_json_store import AtomicJsonFileStore


@dataclass(frozen=True)
class OAuthApplication:
    """CLIO-owned application configuration, provisioned by the distributor."""

    provider: str
    client_id: str
    redirect_uri: str
    authorize_url: str
    token_url: str
    client_secret: str = field(default="", repr=False)


def application(provider: str) -> OAuthApplication:
    """Resolve distributor settings; ordinary users never supply OAuth client details."""
    if provider == "google_drive":
        prefix = "CLIO_STORAGE_GOOGLE"
        authorize = "https://accounts.google.com/o/oauth2/v2/auth"
        token = "https://oauth2.googleapis.com/token"
    elif provider == "globus":
        prefix = "CLIO_STORAGE_GLOBUS"
        authorize = "https://auth.globus.org/v2/oauth2/authorize"
        token = "https://auth.globus.org/v2/oauth2/token"
    else:
        raise ValueError("This source does not use browser sign-in")
    return OAuthApplication(
        provider,
        os.environ.get(prefix + "_CLIENT_ID", ""),
        os.environ.get(
            prefix + "_REDIRECT_URI",
            "https://auth.globus.org/v2/web/auth-code" if provider == "globus" else "",
        ),
        authorize,
        token,
        os.environ.get(prefix + "_CLIENT_SECRET", ""),
    )


@dataclass(repr=False)
class PendingSignIn:
    """Single-use PKCE state bound to the exact approved source and principal."""

    source_id: str
    principal: str
    clio_id: str
    host_id: str
    root: str
    mode: str
    app: OAuthApplication
    state: str
    verifier: str
    expires_at: float


class StorageAuth:
    """Browser-flow owner with sanitized status and a separate private credential store."""

    def __init__(self, path: Path) -> None:
        self.private_root = path.parent
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._vault = AtomicJsonFileStore(
            path, schema="clio-agent.storage-credentials.v1", trace_tag="STORAGE"
        )
        self._pending: dict[str, PendingSignIn] = {}
        self._lock = threading.RLock()

    def connected(self, record: SourceRecord) -> bool:
        """Expose only credential presence for the same principal, CLIO, host and source."""
        with self._lock:
            entry = self._vault.read_entries().get(record.source.id)
        return bool(entry and self._matches(entry, record))

    @staticmethod
    def _matches(entry: dict[str, object], record: SourceRecord) -> bool:
        source = record.source
        return (
            entry.get("principal") == record.principal
            and entry.get("clio_id") == source.owner.clio_id
            and entry.get("host_id") == source.owner.host_id
            and entry.get("provider") == source.provider
            and entry.get("root") == source.root
            and entry.get("mode") == source.mode
        )

    def start(self, record: SourceRecord, app: OAuthApplication | None = None) -> dict[str, str]:
        """Create a bounded PKCE flow; return only the browser URL and opaque flow ID."""
        app = app or application(record.source.provider)
        if app.provider != record.source.provider:
            raise ValueError("The sign-in application does not match this source provider")
        if not app.client_id or not app.redirect_uri:
            raise ValueError(
                "CLIO's storage sign-in application has not been configured by the distributor"
            )
        redirect = urlparse(app.redirect_uri)
        if redirect.scheme != "https" and not (
            redirect.scheme == "http" and redirect.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise ValueError("Storage sign-in requires an approved HTTPS or loopback redirect")
        identifier, state, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        source = record.source
        pending = PendingSignIn(
            source.id,
            record.principal,
            source.owner.clio_id,
            source.owner.host_id,
            source.root,
            source.mode,
            app,
            state,
            verifier,
            time.time() + 600,
        )
        with self._lock:
            self._pending = {
                key: flow
                for key, flow in self._pending.items()
                if flow.expires_at > time.time() and flow.source_id != source.id
            }
            if len(self._pending) >= 100:
                raise ValueError(
                    "Too many pending sign-in attempts; finish or cancel an existing attempt"
                )
            self._pending[identifier] = pending
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        scope = (
            (
                "https://www.googleapis.com/auth/drive.readonly"
                if source.mode == "read_only"
                else "https://www.googleapis.com/auth/drive"
            )
            if app.provider == "google_drive"
            else "urn:globus:auth:scope:transfer.api.globus.org:all offline_access"
        )
        parameters = {
            "client_id": app.client_id,
            "redirect_uri": app.redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if app.provider == "google_drive":
            parameters.update(access_type="offline", prompt="consent")
        return {
            "flow_id": identifier,
            "authorization_url": app.authorize_url + "?" + urlencode(parameters),
        }

    def complete(
        self,
        record: SourceRecord,
        flow_id: str,
        callback_url: str,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        """Exchange a callback URL only for the source and user that initiated it."""
        with self._lock:
            pending = self._pending.get(flow_id)
            if pending is None or pending.expires_at <= time.time():
                raise ValueError("Sign-in expired; start again")
            source = record.source
            if (
                pending.source_id,
                pending.principal,
                pending.clio_id,
                pending.host_id,
                pending.root,
                pending.mode,
            ) != (
                source.id,
                record.principal,
                source.owner.clio_id,
                source.owner.host_id,
                source.root,
                source.mode,
            ):
                raise PermissionError(
                    "This sign-in belongs to another source, user or CLIO connection"
                )
            callback = urlparse(callback_url.strip())
            expected = urlparse(pending.app.redirect_uri)
            native_code = (
                pending.app.provider == "globus"
                and pending.app.redirect_uri == "https://auth.globus.org/v2/web/auth-code"
                and not callback.scheme
                and not callback.query
                and bool(callback_url.strip())
                and len(callback_url.strip()) <= 4096
            )
            if not native_code and (callback.scheme, callback.netloc, callback.path) != (
                expected.scheme,
                expected.netloc,
                expected.path,
            ):
                raise ValueError("Paste the complete URL from CLIO's sign-in return page")
            query = parse_qs(callback.query)
            state = pending.state if native_code else query.get("state", [""])[0]
            code = callback_url.strip() if native_code else query.get("code", [""])[0]
            if not state or not hmac.compare_digest(state, pending.state) or not code:
                raise ValueError("The sign-in response is invalid or was not authorized")
            # Consume before exchanging. A failed network exchange requires a fresh
            # flow instead of permitting concurrent reuse of a one-time code.
            self._pending.pop(flow_id)
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": pending.app.client_id,
            "redirect_uri": pending.app.redirect_uri,
            "code_verifier": pending.verifier,
        }
        if pending.app.client_secret:
            payload["client_secret"] = pending.app.client_secret
        tokens = self._exchange(pending.app, payload, client)
        with self._lock:
            entries = self._vault.read_entries()
            entries[source.id] = {
                "principal": record.principal,
                "clio_id": source.owner.clio_id,
                "host_id": source.owner.host_id,
                "root": source.root,
                "mode": source.mode,
                "provider": source.provider,
                "tokens": tokens,
            }
            self._vault.write_entries(entries)

    @staticmethod
    def _exchange(
        app: OAuthApplication, payload: dict[str, str], client: httpx.Client | None = None
    ) -> dict[str, object]:
        owned = client is None
        client = client or httpx.Client(timeout=30, follow_redirects=False)
        try:
            response = client.post(app.token_url, data=payload)
            if not response.is_success:
                raise ValueError("Storage authorization was rejected; start sign-in again")
            tokens = response.json()
            if app.provider == "globus":
                candidates = [tokens, *tokens.get("other_tokens", [])]
                tokens = next(
                    (
                        row
                        for row in candidates
                        if row.get("resource_server") == "transfer.api.globus.org"
                    ),
                    {},
                )
            if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str):
                raise ValueError("Storage authorization returned no usable access token")
            return {
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", ""),
                "expires_at": time.time() + float(tokens.get("expires_in", 3600)),
            }
        except httpx.HTTPError as exc:
            raise ValueError("The storage sign-in service could not be reached") from exc
        finally:
            if owned:
                client.close()

    def token(self, record: SourceRecord) -> str:
        """Resolve/refresh a credential privately for an approved provider adapter."""
        with self._lock:
            entries = self._vault.read_entries()
            entry = entries.get(record.source.id)
            if not entry or not self._matches(entry, record):
                raise PermissionError("Sign in to this source on the connected CLIO")
            tokens = entry.get("tokens")
            if not isinstance(tokens, dict):
                raise PermissionError("Sign in to this source again")
            if float(tokens.get("expires_at", 0)) < time.time() + 60:
                refresh = tokens.get("refresh_token")
                if not refresh:
                    raise PermissionError("Storage sign-in expired; sign in again")
                app = application(record.source.provider)
                payload = {
                    "grant_type": "refresh_token",
                    "refresh_token": str(refresh),
                    "client_id": app.client_id,
                }
                if app.client_secret:
                    payload["client_secret"] = app.client_secret
                tokens = self._exchange(app, payload)
                if not tokens.get("refresh_token"):
                    tokens["refresh_token"] = refresh
                entry["tokens"] = tokens
                self._vault.write_entries(entries)
            return str(tokens["access_token"])

    def disconnect(self, source_id: str) -> None:
        """Forget this source's credentials and pending flows without deleting source data."""
        with self._lock:
            self._pending = {
                key: value for key, value in self._pending.items() if value.source_id != source_id
            }
            entries = self._vault.read_entries()
            if entries.pop(source_id, None) is not None:
                self._vault.write_entries(entries)
