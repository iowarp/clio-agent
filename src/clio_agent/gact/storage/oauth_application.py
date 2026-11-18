"""Storage sign-in configuration: CLIO's OAuth applications and one pending sign-in."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from clio_agent.gact.storage import github_oauth, ndp_oauth
from clio_agent.gact.storage.oauth_clients import (
    GITHUB_CLIENT_ID,
    GLOBUS_CLIENT_ID,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
)
from clio_agent.gact.storage.oauth_return import StorageOAuthReturn


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
    if provider == "ndp":
        return ndp_oauth.application()
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
    declared_scopes: tuple[str, ...] = ()


__all__ = ["OAuthApplication", "PendingSignIn", "application"]
