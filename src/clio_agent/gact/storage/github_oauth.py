"""GitHub App device authorization for native and remotely hosted CLIO clients."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from clio_agent.gact.storage.oauth_clients import GITHUB_APP_URL, GITHUB_CLIENT_ID

TOKEN_URL = "https://github.com/login/oauth/access_token"


def account_url() -> str:
    """Return the registered app's repository-selection page, or existing installations."""
    default = (
        GITHUB_APP_URL
        if os.environ.get("CLIO_STORAGE_GITHUB_CLIENT_ID", GITHUB_CLIENT_ID) == GITHUB_CLIENT_ID
        else ""
    )
    app = os.environ.get("CLIO_STORAGE_GITHUB_APP_URL", default).rstrip("/")
    if re.fullmatch(r"https://github\.com/apps/[A-Za-z0-9-]+", app):
        return app + "/installations/new"
    return "https://github.com/settings/installations"


@dataclass(repr=False)
class DeviceAuthorization:
    """Keep the device credential private while exposing only the user-facing code."""

    device_code: str = field(repr=False)
    user_code: str
    verification_uri: str
    expires_at: float
    interval: int
    next_poll_at: float = 0


def begin(client_id: str, client: httpx.Client | None = None) -> DeviceAuthorization:
    """Request a GitHub App device code without a client secret or broad OAuth scopes."""
    owned = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        response = client.post(
            "https://github.com/login/device/code",
            data={"client_id": client_id},
            headers={"Accept": "application/json"},
        )
        data = response.json()
        if data.get("error") == "device_flow_disabled":
            raise ValueError("Enable Device flow in the CLIO GitHub App registration")
        if not response.is_success or not data.get("device_code") or not data.get("user_code"):
            raise ValueError(
                "GitHub sign-in could not start; check the CLIO GitHub App registration"
            )
        if data.get("verification_uri") != "https://github.com/login/device":
            raise ValueError("GitHub returned an unexpected sign-in address")
        return DeviceAuthorization(
            str(data["device_code"]),
            str(data["user_code"]),
            data["verification_uri"],
            time.time() + min(int(data.get("expires_in", 900)), 900),
            max(5, int(data.get("interval", 5))),
        )
    except httpx.HTTPError as exc:
        raise ValueError("GitHub sign-in could not be reached; try again") from exc
    finally:
        if owned:
            client.close()


def token_bundle(data: dict[str, Any]) -> dict[str, object]:
    """Normalize GitHub expiring and non-expiring user grants for the shared vault."""
    if not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise ValueError("GitHub authorization was rejected; sign in again")
    result: dict[str, object] = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "non_expiring": "expires_in" not in data,
        "expires_at": time.time() + float(data.get("expires_in", 0)),
    }
    if "refresh_token_expires_in" in data:
        result["refresh_expires_at"] = time.time() + float(data["refresh_token_expires_in"])
    return result


def poll(
    flow: DeviceAuthorization, client_id: str, client: httpx.Client | None = None
) -> dict[str, object] | None:
    """Respect GitHub's polling interval and return a token only after user approval."""
    if flow.expires_at <= time.time():
        raise ValueError("GitHub sign-in expired; start again")
    if flow.next_poll_at > time.time():
        return None
    flow.next_poll_at = time.time() + flow.interval
    owned = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        response = client.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "device_code": flow.device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
        )
        data = response.json()
        error = data.get("error")
        if error == "authorization_pending":
            return None
        if error == "slow_down":
            flow.interval = max(flow.interval + 5, int(data.get("interval", 0)))
            flow.next_poll_at = time.time() + flow.interval
            return None
        if error == "access_denied":
            raise ValueError("GitHub sign-in was cancelled")
        if error == "expired_token":
            raise ValueError("GitHub sign-in expired; start again")
        if not response.is_success or error:
            raise ValueError("GitHub authorization was rejected; sign in again")
        result = token_bundle(data)
        result["client_id"] = client_id
        return result
    except httpx.HTTPError as exc:
        raise ValueError("GitHub sign-in could not be reached; try again") from exc
    finally:
        if owned:
            client.close()
