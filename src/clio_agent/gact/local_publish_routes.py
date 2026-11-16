"""Route glue for publishing the private marketplace: the signed-in account and errors."""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI, HTTPException

from clio_agent.gact.agents.resolution import plugin_service
from clio_agent.gact.local_publish import PublishError, publish, status
from clio_agent.gact.storage.github_oauth import account_url

GITHUB = "github"


def publish_error(exc: PublishError) -> HTTPException:
    """The standard typed GACT error envelope for a refused publication."""
    return HTTPException(
        status_code=exc.status,
        detail={
            "error": {
                "error": exc.code,
                "message": str(exc),
                "details": exc.details,
                "recoverable": exc.status < 500,
            }
        },
    )


def _account(app: FastAPI) -> tuple[Any, bool, str]:
    storage = app.state.connected_storage
    signed_in = storage.auth.account_connected(storage.principal, storage.store.clio_id, GITHUB)
    name = storage.auth.account_name(storage.principal, storage.store.clio_id, GITHUB)
    return storage, signed_in, name if signed_in else ""


def publish_status(app: FastAPI) -> dict[str, Any]:
    """Who is signed in to GitHub here, and the last publication."""
    _storage, signed_in, name = _account(app)
    try:
        current = status(plugin_service(app))
    except PublishError as exc:
        raise publish_error(exc) from exc
    return {
        "github": {
            "signed_in": signed_in,
            "account_name": name,
            "account_url": account_url(),
        },
        **current,
    }


def publish_to_github(app: FastAPI, repository: str, allow_public: bool) -> dict[str, Any]:
    """Publish with the person's own GitHub sign-in on this CLIO."""
    storage = app.state.connected_storage
    try:
        token, _expires = storage.auth.account_token(
            storage.principal, storage.store.clio_id, GITHUB
        )
    except PermissionError as exc:
        raise publish_error(
            PublishError(409, "github_sign_in_required", "Sign in to GitHub to publish.")
        ) from exc
    try:
        return publish(
            plugin_service(app),
            token,
            repository,
            allow_public=allow_public,
            account_url=account_url(),
        )
    except PublishError as exc:
        raise publish_error(exc) from exc
    except httpx.HTTPError as exc:
        raise publish_error(
            PublishError(502, "github_unavailable", f"GitHub could not be reached: {exc}")
        ) from exc
