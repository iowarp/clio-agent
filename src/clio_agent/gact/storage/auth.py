"""Trusted storage sign-in. Credentials never enter source records or agent tool results."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from clio_schemas.connected_resources import ConnectedSource, ResourceOwner, SourceCapabilities

from clio_agent.gact.storage import github_oauth
from clio_agent.gact.storage.accounts import (
    Entries,
    account_for,
    migrate_accounts,
    owned_by,
    source_binding,
    usable,
)
from clio_agent.gact.storage.globus_consent import TRANSFER_SCOPE, collection_scopes
from clio_agent.gact.storage.models import SftpCredentials, SourceRecord
from clio_agent.gact.storage.oauth_clients import (
    GITHUB_CLIENT_ID,
    GLOBUS_CLIENT_ID,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
)
from clio_agent.gact.storage.oauth_return import StorageOAuthReturn
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
    """Use CLIO's native registration, with optional distributor overrides."""
    if provider == "google_drive":
        prefix = "CLIO_STORAGE_GOOGLE"
        authorize = "https://accounts.google.com/o/oauth2/v2/auth"
        token = "https://oauth2.googleapis.com/token"
        default_client_id = GOOGLE_CLIENT_ID
        default_client_secret = GOOGLE_CLIENT_SECRET
        # Desktop replaces this qualification fallback with its loopback receiver.
        default_redirect = "http://127.0.0.1:48173/clio-storage-return"
    elif provider == "globus":
        prefix = "CLIO_STORAGE_GLOBUS"
        authorize = "https://auth.globus.org/v2/oauth2/authorize"
        token = "https://auth.globus.org/v2/oauth2/token"
        default_client_id = GLOBUS_CLIENT_ID
        default_client_secret = ""
        default_redirect = "https://auth.globus.org/v2/web/auth-code"
    elif provider == "github":
        prefix = "CLIO_STORAGE_GITHUB"
        authorize = default_redirect = "https://github.com/login/device"
        token = github_oauth.TOKEN_URL
        default_client_id = GITHUB_CLIENT_ID
        default_client_secret = ""
    else:
        raise ValueError("This source does not use browser sign-in")
    client_id = os.environ.get(prefix + "_CLIENT_ID", default_client_id)
    # A custom client must supply its own matching value; never mix registrations.
    client_secret = os.environ.get(
        prefix + "_CLIENT_SECRET", default_client_secret if client_id == default_client_id else ""
    )
    return OAuthApplication(
        provider,
        client_id,
        os.environ.get(prefix + "_REDIRECT_URI", default_redirect),
        authorize,
        token,
        client_secret,
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
    device: github_oauth.DeviceAuthorization | None = field(default=None, repr=False)
    receiver: StorageOAuthReturn | None = field(default=None, repr=False)
    write_access: bool = False


class StorageAuth:
    """Browser-flow owner with sanitized status and a separate private credential store."""

    def __init__(self, path: Path) -> None:
        self.private_root = path.parent
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._vault = AtomicJsonFileStore(
            path, schema="clio-agent.storage-credentials.v1", trace_tag="STORAGE"
        )
        self._pending: dict[str, PendingSignIn] = {}
        self._exchanging: dict[str, PendingSignIn] = {}
        self._sftp_credentials: dict[str, tuple[dict[str, object], SftpCredentials]] = {}
        self._lock = threading.RLock()

    def connected(self, record: SourceRecord) -> bool:
        """Report usable account access and any source-specific consent still needed."""
        if record.source.provider == "sftp":
            with self._lock:
                sftp_entry = self._sftp_credentials.get(record.source.id)
                return bool(sftp_entry and self._matches(sftp_entry[0], record))
        with self._lock:
            entries = self._entries()
            account = self._account(entries, record)
            binding = entries.get(record.source.id, {})
            return bool(account and not binding.get("consent_required"))

    @staticmethod
    def account_login(principal: str, clio_id: str, provider: str) -> SourceRecord:
        """Bind provider-only OAuth to this user and host without creating a workspace source."""
        if provider not in {"google_drive", "globus", "github"}:
            raise ValueError("This provider does not use browser sign-in")
        return SourceRecord(
            principal=principal,
            source=ConnectedSource(
                id="account-login-"
                + hashlib.sha256(f"{principal}\0{clio_id}\0{provider}".encode()).hexdigest(),
                provider=cast(Literal["google_drive", "globus", "github"], provider),
                label=provider,
                root="account",
                owner=ResourceOwner(clio_id=clio_id, host_id="local"),
                capabilities=SourceCapabilities(),
            ),
        )

    def _entries(self) -> Entries:
        entries = self._vault.read_entries()
        if migrate_accounts(entries):
            self._vault.write_entries(entries)
        return entries

    def _account(self, entries: Entries, record: SourceRecord) -> dict[str, object] | None:
        selected = account_for(entries, record)
        if selected is None:
            return None
        identifier, account = selected
        if not entries.get(record.source.id, {}).get("account_id"):
            entries[record.source.id] = {**source_binding(record), "account_id": identifier}
            self._vault.write_entries(entries)
        return account

    def account_connected(
        self, principal: str, clio_id: str, provider: str, host_id: str = "local"
    ) -> bool:
        """Expose account status across workspaces without exposing account credentials."""
        with self._lock:
            return any(
                entry.get("kind") == "account"
                and owned_by(entry, principal, clio_id, host_id, provider)
                and usable(entry)
                for entry in self._entries().values()
            )

    def sign_out(self, principal: str, clio_id: str, provider: str, host_id: str = "local") -> None:
        """Forget this user's provider logins on this CLIO, across all workspaces."""
        with self._lock:
            entries = self._entries()
            entries = {
                key: value
                for key, value in entries.items()
                if not owned_by(value, principal, clio_id, host_id, provider)
            }
            for key, flow in list(self._pending.items()):
                if (flow.principal, flow.clio_id, flow.host_id, flow.app.provider) == (
                    principal,
                    clio_id,
                    host_id,
                    provider,
                ):
                    self._remove_pending(key)
            self._exchanging = {
                key: flow
                for key, flow in self._exchanging.items()
                if (flow.principal, flow.clio_id, flow.host_id, flow.app.provider)
                != (principal, clio_id, host_id, provider)
            }
            self._vault.write_entries(entries)

    def reset_source(self, source_id: str) -> None:
        """Clear only source consent and account selection when its setup changes."""
        with self._lock:
            self.disconnect(source_id)
            entries = self._entries()
            entries.pop(source_id, None)
            self._vault.write_entries(entries)

    def reject_token(self, record: SourceRecord, token: str) -> None:
        """Invalidate a rejected grant only if it has not been replaced by a newer login."""
        with self._lock:
            entries = self._entries()
            account = self._account(entries, record)
            tokens = account.get("tokens") if account else None
            if account and isinstance(tokens, dict) and tokens.get("access_token") == token:
                account["invalid"] = True
                self._vault.write_entries(entries)

    def reconnect(self, source_id: str) -> None:
        """Restore source access using its retained account, without another login."""
        with self._lock:
            entries = self._entries()
            if source_id in entries:
                entries[source_id].pop("disconnected", None)
                self._vault.write_entries(entries)

    def save_sftp_credentials(self, record: SourceRecord, credentials: SftpCredentials) -> None:
        """Keep source-bound SFTP credentials in memory only, never on disk or in logs."""
        if record.source.provider != "sftp":
            raise ValueError("SFTP credentials require an SFTP source")
        if record.configuration.ssh_authentication == "password" and not credentials.password:
            raise ValueError("Enter the SFTP password")
        if record.configuration.ssh_authentication == "key" and not credentials.private_key:
            raise ValueError("Choose or paste an SSH private key")
        source = record.source
        binding: dict[str, object] = {
            "principal": record.principal,
            "clio_id": source.owner.clio_id,
            "host_id": source.owner.host_id,
            "provider": source.provider,
            "root": source.root,
            "mode": source.mode,
        }
        with self._lock:
            self._sftp_credentials[source.id] = (binding, credentials)

    def sftp_credentials(self, record: SourceRecord) -> SftpCredentials | None:
        """Resolve this source's live credentials; configured-key sources need none."""
        if record.configuration.ssh_authentication == "configured":
            return None
        with self._lock:
            entry = self._sftp_credentials.get(record.source.id)
            if not entry or not self._matches(entry[0], record):
                raise PermissionError("Sign in to this SFTP source again on the connected CLIO")
            return entry[1]

    def require_globus_consent(self, record: SourceRecord, scopes: list[str]) -> None:
        """Retain credentials while requesting consent only for approved collections."""
        scopes = collection_scopes(record, scopes)
        with self._lock:
            entries = self._entries()
            account = self._account(entries, record)
            entry = entries.get(record.source.id)
            if not account or not entry or not self._matches(entry, record):
                raise PermissionError("Sign in to this source on the connected CLIO")
            previous = entry.get("consent_scopes", [])
            if not isinstance(previous, list) or any(
                not isinstance(scope, str) for scope in previous
            ):
                raise ValueError("Invalid saved Globus collection consent")
            scopes = collection_scopes(record, [*previous, *scopes])
            entry.update(consent_required=True, consent_scopes=scopes)
            self._vault.write_entries(entries)

    def _globus_scopes(self, record: SourceRecord) -> str:
        with self._lock:
            entry = self._entries().get(record.source.id)
        if entry and self._matches(entry, record) and entry.get("consent_scopes"):
            scopes = entry["consent_scopes"]
            if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
                raise ValueError("Invalid saved Globus collection consent")
            scopes = collection_scopes(record, scopes)
            if not any(scope.startswith(TRANSFER_SCOPE) for scope in scopes):
                scopes.insert(0, TRANSFER_SCOPE)
            return " ".join(scopes) + " offline_access"
        return TRANSFER_SCOPE + " offline_access"

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

    def start(
        self,
        record: SourceRecord,
        app: OAuthApplication | None = None,
        *,
        desktop_redirect: str | None = None,
        local_browser: bool = False,
    ) -> dict[str, Any]:
        """Create a bounded PKCE flow; return only the browser URL and opaque flow ID."""
        app = app or application(record.source.provider)
        if desktop_redirect:
            callback = urlparse(desktop_redirect)
            valid_google = (
                record.source.provider == "google_drive"
                and callback.scheme == "http"
                and callback.hostname == "127.0.0.1"
                and callback.port is not None
                and callback.path == "/clio-storage-return"
                and not callback.username
                and not callback.password
                and not callback.query
                and not callback.fragment
            )
            valid_globus = (
                record.source.provider == "globus"
                and desktop_redirect == "http://localhost:48173/clio-storage-return"
            )
            if not valid_google and not valid_globus:
                raise ValueError("Invalid Desktop sign-in return address")
            app = replace(app, redirect_uri=desktop_redirect)
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
        pending.write_access = record.linked_access != "read_only"
        with self._lock:
            for key, flow in list(self._pending.items()):
                if flow.expires_at <= time.time() or flow.source_id == source.id:
                    self._remove_pending(key)
            if len(self._pending) >= 100:
                raise ValueError(
                    "Too many pending sign-in attempts; finish or cancel an existing attempt"
                )
            if local_browser and not desktop_redirect and app.provider == "google_drive":
                pending.receiver = StorageOAuthReturn(state, pending.expires_at)
                app = replace(app, redirect_uri=pending.receiver.redirect_uri)
                pending.app = app
            self._pending[identifier] = pending
        if app.provider == "github":
            try:
                device = github_oauth.begin(app.client_id)
            except Exception:
                with self._lock:
                    self._pending.pop(identifier, None)
                raise
            with self._lock:
                if self._pending.get(identifier) is not pending:
                    raise PermissionError("Sign-in was cancelled; start again")
                pending.device = device
                pending.expires_at = device.expires_at
            return {
                "flow_id": identifier,
                "authorization_url": device.verification_uri,
                "user_code": device.user_code,
                "interval": device.interval,
            }
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        scope = (
            (
                "https://www.googleapis.com/auth/drive.readonly"
                if record.linked_access == "read_only"
                else "https://www.googleapis.com/auth/drive"
            )
            if app.provider == "google_drive"
            else self._globus_scopes(record)
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
            "automatic_callback": pending.receiver is not None,
        }

    def _remove_pending(self, identifier: str) -> PendingSignIn | None:
        pending = self._pending.pop(identifier, None)
        if pending is not None and pending.receiver is not None:
            pending.receiver.close()
        return pending

    def complete(
        self,
        record: SourceRecord,
        flow_id: str,
        callback_url: str,
        *,
        client: httpx.Client | None = None,
    ) -> bool:
        """Exchange a callback URL only for the source and user that initiated it."""
        with self._lock:
            pending = self._pending.get(flow_id)
            if pending is None or pending.expires_at <= time.time():
                self._remove_pending(flow_id)
                raise ValueError("Sign-in expired; start again")
            source = record.source
            if pending.app.provider == "google_drive" and pending.write_access != (
                record.linked_access != "read_only"
            ):
                raise PermissionError("The requested folder access changed; start sign-in again")
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
            if pending.device is not None:
                return self._complete_device(record, flow_id, pending, client)
            if pending.receiver is not None and not callback_url.strip():
                callback_url = pending.receiver.result()
                if not callback_url:
                    return False
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
            if hmac.compare_digest(state, pending.state) and query.get("error"):
                self._remove_pending(flow_id)
                raise ValueError("Sign-in was not approved. Try again when you are ready.")
            if not state or not hmac.compare_digest(state, pending.state) or not code:
                raise ValueError("The sign-in response is invalid or was not authorized")
            # Consume before exchanging. A failed network exchange requires a fresh
            # flow instead of permitting concurrent reuse of a one-time code.
            self._remove_pending(flow_id)
            self._exchanging[flow_id] = pending
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": pending.app.client_id,
            "redirect_uri": pending.app.redirect_uri,
            "code_verifier": pending.verifier,
        }
        if pending.app.client_secret:
            payload["client_secret"] = pending.app.client_secret
        try:
            tokens = self._exchange(
                pending.app, payload, client, collection_id=record.configuration.collection_id
            )
        except Exception:
            with self._lock:
                self._exchanging.pop(flow_id, None)
            raise
        with self._lock:
            if self._exchanging.pop(flow_id, None) is not pending:
                raise PermissionError("Sign-in was cancelled; start again")
            entries = self._entries()
            previous = entries.get(source.id)
            consent_scopes = (
                previous.get("consent_scopes", [])
                if previous and self._matches(previous, record)
                else []
            )
            account_id = "account_" + secrets.token_hex(16)
            entries[account_id] = {
                **source_binding(record),
                "kind": "account",
                "mode": "working_copy"
                if pending.write_access
                and (
                    "scope" not in tokens
                    or "https://www.googleapis.com/auth/drive" in str(tokens["scope"]).split()
                )
                else "read_only",
                "tokens": tokens,
                "saved_at": time.time(),
            }
            entries[source.id] = {
                **source_binding(record),
                "account_id": account_id,
                "consent_scopes": consent_scopes,
            }
            self._vault.write_entries(entries)
        return True

    def _complete_device(
        self,
        record: SourceRecord,
        flow_id: str,
        pending: PendingSignIn,
        client: httpx.Client | None,
    ) -> bool:
        """Poll under the auth lock so disconnect/sign-out cannot race a saved grant."""
        assert pending.device is not None
        try:
            tokens = github_oauth.poll(pending.device, pending.app.client_id, client)
        except Exception:
            self._pending.pop(flow_id, None)
            raise
        if tokens is None:
            return False
        if self._pending.get(flow_id) is not pending:
            raise PermissionError("This sign-in was cancelled")
        self._pending.pop(flow_id, None)
        entries = self._entries()
        account_id = "account_" + secrets.token_hex(16)
        entries[account_id] = {
            **source_binding(record),
            "kind": "account",
            "tokens": tokens,
            "saved_at": time.time(),
        }
        entries[record.source.id] = {**source_binding(record), "account_id": account_id}
        self._vault.write_entries(entries)
        return True

    @staticmethod
    def _exchange(
        app: OAuthApplication,
        payload: dict[str, str],
        client: httpx.Client | None = None,
        *,
        collection_id: str = "",
        resource_server: str = "transfer.api.globus.org",
    ) -> dict[str, object]:
        owned = client is None
        client = client or httpx.Client(timeout=30, follow_redirects=False)
        try:
            response = client.post(
                app.token_url, data=payload, headers={"Accept": "application/json"}
            )
            if not response.is_success:
                raise ValueError("Storage authorization was rejected; start sign-in again")
            tokens = response.json()
            if app.provider == "github":
                return github_oauth.token_bundle(tokens)
            if app.provider == "globus":
                candidates = [tokens, *tokens.get("other_tokens", [])]
                tokens = next(
                    (row for row in candidates if row.get("resource_server") == resource_server),
                    {},
                )
            if not isinstance(tokens, dict) or not isinstance(tokens.get("access_token"), str):
                raise ValueError("Storage authorization returned no usable access token")
            result: dict[str, object] = {
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", ""),
                "expires_at": time.time() + float(tokens.get("expires_in", 3600)),
            }
            if "scope" in tokens:
                result["scope"] = str(tokens["scope"])
            if app.provider == "globus" and collection_id:
                result["collection_tokens"] = {
                    row["resource_server"]: {
                        "access_token": row["access_token"],
                        "refresh_token": row.get("refresh_token", ""),
                        "expires_at": time.time() + float(row.get("expires_in", 3600)),
                    }
                    for row in candidates
                    if row.get("resource_server") == collection_id
                    and isinstance(row.get("access_token"), str)
                    and f"https://auth.globus.org/scopes/{collection_id}/https"
                    in str(row.get("scope", "")).split()
                }
            return result
        except httpx.HTTPError as exc:
            raise ValueError("The storage sign-in service could not be reached") from exc
        finally:
            if owned:
                client.close()

    def token(self, record: SourceRecord, *, resource_server: str | None = None) -> str:
        """Resolve/refresh a credential privately for an approved provider adapter."""
        with self._lock:
            entries = self._entries()
            entry = self._account(entries, record)
            if not entry:
                raise PermissionError("Sign in to this source on the connected CLIO")
            tokens = entry.get("tokens")
            if not isinstance(tokens, dict):
                raise PermissionError("Sign in to this source again")
            bundle = tokens
            if resource_server is not None:
                if (
                    record.source.provider != "globus"
                    or resource_server != record.configuration.collection_id
                ):
                    raise PermissionError("The credential belongs to another collection")
                collections = bundle.get("collection_tokens", {})
                tokens = collections.get(resource_server) if isinstance(collections, dict) else None
                if not isinstance(tokens, dict):
                    raise PermissionError("Authorize downloads from this collection")
            if (
                not tokens.get("non_expiring")
                and float(tokens.get("expires_at", 0)) < time.time() + 60
            ):
                refresh = tokens.get("refresh_token")
                if not refresh:
                    raise PermissionError("Storage sign-in expired; sign in again")
                app = application(record.source.provider)
                if record.source.provider == "github" and tokens.get("client_id") != app.client_id:
                    raise PermissionError("The GitHub application changed; sign in again")
                payload = {
                    "grant_type": "refresh_token",
                    "refresh_token": str(refresh),
                    "client_id": app.client_id,
                }
                if app.client_secret:
                    payload["client_secret"] = app.client_secret
                try:
                    refreshed = self._exchange(
                        app, payload, resource_server=resource_server or "transfer.api.globus.org"
                    )
                except ValueError as exc:
                    if "authorization was rejected" in str(exc):
                        entry["invalid"] = True
                        self._vault.write_entries(entries)
                    raise
                # A resource-server refresh must not discard the other server's tokens.
                tokens.update(refreshed)
                if not tokens.get("refresh_token"):
                    tokens["refresh_token"] = refresh
                self._vault.write_entries(entries)
            return str(tokens["access_token"])

    def disconnect(self, source_id: str) -> None:
        """Stop this source and its pending flows, retaining reusable browser accounts."""
        with self._lock:
            self._sftp_credentials.pop(source_id, None)
            for key, value in list(self._pending.items()):
                if value.source_id == source_id:
                    self._remove_pending(key)
            self._exchanging = {
                key: value
                for key, value in self._exchanging.items()
                if value.source_id != source_id
            }
            entries = self._entries()
            if source_id in entries:
                entries[source_id]["disconnected"] = True
                self._vault.write_entries(entries)
