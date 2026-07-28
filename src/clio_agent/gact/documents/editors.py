"""ONLYOFFICE and Collabora editor configuration and scoped access tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from fastapi import FastAPI

_TOKEN_TTL_SECONDS = 15 * 60
_WOPI_LOCK_TTL_SECONDS = 30 * 60


@dataclass(frozen=True)
class EditorEndpoint:
    """One explicitly configured embedded editor endpoint."""

    provider: str
    url: str
    configured: bool
    healthy: bool
    error: str = ""


@dataclass(frozen=True)
class WopiLock:
    """One expiring WOPI editor lock."""

    value: str
    expires_at: float


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token_secret(app: "FastAPI") -> bytes:
    existing = getattr(app.state, "document_token_secret", None)
    if isinstance(existing, bytes) and existing:
        return existing
    secret = secrets.token_bytes(32)
    app.state.document_token_secret = secret
    return secret


def _wopi_lock_state(app: "FastAPI") -> tuple[threading.RLock, dict[str, WopiLock]]:
    lock = getattr(app.state, "document_wopi_lock_guard", None)
    rows = getattr(app.state, "document_wopi_locks", None)
    if isinstance(lock, type(threading.RLock())) and isinstance(rows, dict):
        return lock, rows
    lock = threading.RLock()
    rows = {}
    app.state.document_wopi_lock_guard = lock
    app.state.document_wopi_locks = rows
    return lock, rows


def wopi_current_lock(app: "FastAPI", working_copy_id: str) -> str:
    """Return the active WOPI lock, removing an expired lock first."""

    guard, rows = _wopi_lock_state(app)
    with guard:
        current = rows.get(working_copy_id)
        if current is not None and current.expires_at <= time.monotonic():
            rows.pop(working_copy_id, None)
            return ""
        return current.value if current is not None else ""


def wopi_acquire_lock(
    app: "FastAPI",
    working_copy_id: str,
    value: str,
    *,
    old_value: str = "",
) -> tuple[bool, str]:
    """Acquire, refresh, or atomically replace a WOPI lock."""

    guard, rows = _wopi_lock_state(app)
    with guard:
        current = wopi_current_lock(app, working_copy_id)
        if old_value:
            if current != old_value:
                return False, current
        elif current and current != value:
            return False, current
        rows[working_copy_id] = WopiLock(
            value=value,
            expires_at=time.monotonic() + _WOPI_LOCK_TTL_SECONDS,
        )
        return True, value


def wopi_refresh_lock(app: "FastAPI", working_copy_id: str, value: str) -> tuple[bool, str]:
    """Refresh a matching WOPI lock for another 30 minutes."""

    guard, rows = _wopi_lock_state(app)
    with guard:
        current = wopi_current_lock(app, working_copy_id)
        if not current or current != value:
            return False, current
        rows[working_copy_id] = WopiLock(
            value=value,
            expires_at=time.monotonic() + _WOPI_LOCK_TTL_SECONDS,
        )
        return True, value


def wopi_release_lock(app: "FastAPI", working_copy_id: str, value: str) -> tuple[bool, str]:
    """Release a matching WOPI lock."""

    guard, rows = _wopi_lock_state(app)
    with guard:
        current = wopi_current_lock(app, working_copy_id)
        if not current or current != value:
            return False, current
        rows.pop(working_copy_id, None)
        return True, ""


def issue_access_token(
    app: "FastAPI",
    *,
    working_copy_id: str,
    provider: str,
    writable: bool,
) -> tuple[str, int]:
    """Issue one short-lived, working-copy-scoped HMAC access token."""

    expires = int(time.time()) + _TOKEN_TTL_SECONDS
    payload = {
        "wc": working_copy_id,
        "provider": provider,
        "write": writable,
        "exp": expires,
        "nonce": secrets.token_hex(8),
    }
    body = _base64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _base64url(
        hmac.new(_token_secret(app), body.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{body}.{signature}", expires


def verify_access_token(
    app: "FastAPI",
    token: str,
    *,
    working_copy_id: str,
    provider: str,
    require_write: bool = False,
) -> dict[str, Any]:
    """Validate a scoped access token or raise ``ValueError``."""

    try:
        body, supplied_signature = token.split(".", 1)
        expected_signature = _base64url(
            hmac.new(_token_secret(app), body.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("invalid signature")
        padding = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + padding))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid editor access token") from exc
    if (
        payload.get("wc") != working_copy_id
        or payload.get("provider") != provider
        or int(payload.get("exp", 0)) < int(time.time())
    ):
        raise ValueError("expired or incorrectly scoped editor access token")
    if require_write and not payload.get("write"):
        raise ValueError("editor access token is read-only")
    return payload


def editor_url(provider: str) -> str:
    """Return the configured editor base URL for ``provider``."""

    if provider == "onlyoffice":
        value = os.environ.get("CLIO_ONLYOFFICE_URL", "")
    elif provider == "collabora":
        value = os.environ.get("CLIO_COLLABORA_URL", "")
    else:
        raise ValueError(f"unsupported document editor provider: {provider}")
    return value.strip().rstrip("/")


def public_gact_url() -> str:
    """Return the editor-reachable public GACT base URL."""

    return (
        os.environ.get("CLIO_GACT_PUBLIC_URL", "http://host.docker.internal:8000")
        .strip()
        .rstrip("/")
    )


def protected_editor_url(url: str) -> bool:
    """Reject malformed editor endpoints and URLs with embedded credentials."""

    parsed = urlparse(url)
    return (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    )


def endpoint_health(provider: str) -> EditorEndpoint:
    """Probe an explicitly configured editor endpoint with a bounded request."""

    url = editor_url(provider)
    if not url:
        return EditorEndpoint(provider, "", False, False, "endpoint is not configured")
    if protected_editor_url(url):
        return EditorEndpoint(provider, url, True, False, "endpoint URL is invalid")
    health_path = "/healthcheck" if provider == "onlyoffice" else "/hosting/discovery"
    try:
        request = Request(f"{url}{health_path}", headers={"User-Agent": "clio-agent"})
        with urlopen(request, timeout=3.0) as response:  # noqa: S310 - configured endpoint
            healthy = 200 <= int(response.status) < 400
    except OSError as exc:
        return EditorEndpoint(provider, url, True, False, str(exc))
    return EditorEndpoint(provider, url, True, healthy, "" if healthy else "health check failed")


def onlyoffice_jwt(payload: dict[str, Any]) -> str:
    """Sign an ONLYOFFICE configuration JWT when a shared secret is configured."""

    secret = os.environ.get("CLIO_ONLYOFFICE_JWT_SECRET", "")
    if not secret:
        return ""
    header = _base64url(b'{"alg":"HS256","typ":"JWT"}')
    body = _base64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{header}.{body}"
    signature = _base64url(
        hmac.new(secret.encode(), signing_input.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{signing_input}.{signature}"


__all__ = [
    "EditorEndpoint",
    "editor_url",
    "endpoint_health",
    "issue_access_token",
    "onlyoffice_jwt",
    "protected_editor_url",
    "public_gact_url",
    "verify_access_token",
    "wopi_acquire_lock",
    "wopi_current_lock",
    "wopi_refresh_lock",
    "wopi_release_lock",
]
