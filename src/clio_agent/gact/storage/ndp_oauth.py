"""NDP's public Keycloak device client with offline refresh and PKCE."""

from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from clio_agent.gact.storage.github_oauth import DeviceAuthorization

if TYPE_CHECKING:
    from clio_agent.gact.storage.auth import OAuthApplication

DEFAULT_ISSUER = "https://idp-test.nationaldataplatform.org/realms/NDP"


def configuration() -> tuple[str, str]:
    """Return distributor overrides; cilo is the registered public client's spelling."""
    return (
        os.environ.get("CLIO_STORAGE_NDP_ISSUER", DEFAULT_ISSUER).rstrip("/"),
        os.environ.get("CLIO_STORAGE_NDP_CLIENT_ID", "cilo"),
    )


def https_url(value: str) -> str:
    """Accept an HTTPS address without embedded credentials or fragments."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("NDP requires an HTTPS address without credentials")
    return value


@lru_cache(maxsize=8)
def discovery(issuer: str, bucket: int) -> dict[str, Any]:
    """Cache public discovery briefly and pin credential destinations to the issuer origin."""
    https_url(issuer)
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.get(issuer + "/.well-known/openid-configuration")
            response.raise_for_status()
            data = response.json()
        if data.get("issuer") != issuer:
            raise ValueError("NDP discovery returned a different issuer")
        for key in (
            "device_authorization_endpoint",
            "token_endpoint",
            "revocation_endpoint",
            "userinfo_endpoint",
        ):
            if key not in data and key in {"revocation_endpoint", "userinfo_endpoint"}:
                continue
            target = https_url(str(data.get(key, "")))
            if urlsplit(target).netloc != urlsplit(issuer).netloc:
                raise ValueError("NDP discovery returned a credential destination on another host")
        return data
    except (httpx.HTTPError, TypeError, KeyError) as exc:
        raise ValueError("NDP sign-in discovery could not be reached") from exc


def application() -> OAuthApplication:
    """Build device authorization settings from the realm's discovery document."""
    from clio_agent.gact.storage.auth import OAuthApplication

    issuer, client_id = configuration()
    data = discovery(issuer, int(time.time() // 300))
    return OAuthApplication(
        "ndp", client_id, issuer, data["device_authorization_endpoint"], data["token_endpoint"]
    )


@dataclass(repr=False)
class NDPDevice(DeviceAuthorization):
    """Keep the device credential private while retaining the complete verification link."""


def begin(app: OAuthApplication, verifier: str) -> NDPDevice:
    """Start the tested device flow with S256 instead of the AAI's non-PKCE wrappers."""
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            response = client.post(
                app.authorize_url,
                data={
                    "client_id": app.client_id,
                    "scope": "openid offline_access",
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                },
            )
            if not response.is_success:
                raise ValueError(
                    "NDP device sign-in could not start; check the public client registration"
                )
            data = response.json()
        verification = https_url(
            str(data.get("verification_uri_complete") or data["verification_uri"])
        )
        if urlsplit(verification).netloc != urlsplit(app.redirect_uri).netloc:
            raise ValueError("NDP returned a sign-in address on another host")
        if not data.get("device_code") or not data.get("user_code"):
            raise ValueError("NDP did not return a device sign-in code")
        lifetime = max(1, min(int(data["expires_in"]), 3600))
        return NDPDevice(
            str(data["device_code"]),
            str(data["user_code"]),
            verification,
            time.time() + lifetime,
            max(1, int(data.get("interval", 5))),
        )
    except (httpx.HTTPError, KeyError, TypeError) as exc:
        raise ValueError("NDP device sign-in could not start") from exc


def poll(
    flow: DeviceAuthorization,
    app: OAuthApplication,
    verifier: str,
    client: httpx.Client | None = None,
) -> dict[str, object] | None:
    """Honor polling limits and return refreshable credentials only after approval."""
    if flow.expires_at <= time.time():
        raise ValueError("NDP sign-in expired; start again")
    if time.time() < flow.next_poll_at:
        return None
    flow.next_poll_at = time.time() + flow.interval
    owned = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        response = client.post(
            app.token_url,
            data={
                "client_id": app.client_id,
                "device_code": flow.device_code,
                "code_verifier": verifier,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        data = response.json()
        error = data.get("error")
        if error == "authorization_pending":
            return None
        if error == "slow_down":
            flow.interval += 5
            flow.next_poll_at = time.time() + flow.interval
            return None
        if error == "access_denied":
            raise ValueError("NDP sign-in was declined")
        if error == "expired_token":
            raise ValueError("NDP sign-in expired; start again")
        if (
            not response.is_success
            or error
            or not data.get("access_token")
            or not data.get("refresh_token")
        ):
            raise ValueError("NDP did not issue an offline grant; sign in again")
        result: dict[str, object] = {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": time.time() + float(data.get("expires_in", 300)),
            "issuer": app.redirect_uri,
            "client_id": app.client_id,
        }
        # A profile service outage must not discard an already issued offline grant.
        try:
            info_url = discovery(app.redirect_uri, int(time.time() // 300)).get("userinfo_endpoint")
            if info_url:
                info = client.get(
                    info_url, headers={"Authorization": "Bearer " + str(data["access_token"])}
                )
                if info.is_success:
                    result["username"] = str(info.json().get("preferred_username", ""))
        except (httpx.HTTPError, ValueError):
            pass
        return result
    except httpx.HTTPError as exc:
        raise ValueError("NDP sign-in could not be reached; try again") from exc
    finally:
        if owned:
            client.close()


def revoke(tokens: dict[str, object]) -> None:
    """Best-effort revoke before forgetting; only send credentials to their original realm."""
    issuer, client_id = configuration()
    if tokens.get("issuer") != issuer or tokens.get("client_id") != client_id:
        return
    try:
        endpoint = discovery(issuer, int(time.time() // 300)).get("revocation_endpoint")
        if endpoint and tokens.get("refresh_token"):
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                client.post(
                    endpoint,
                    data={
                        "client_id": client_id,
                        "token": str(tokens["refresh_token"]),
                        "token_type_hint": "refresh_token",
                    },
                )
    except (httpx.HTTPError, ValueError):
        # Local sign-out must succeed even while the realm is unavailable.
        return
