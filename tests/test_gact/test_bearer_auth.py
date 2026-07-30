"""Bearer authentication for remote GACT clients."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from tests._config_layer import delete_config, set_config

_BEARER_TOKEN = "correct-horse-battery-staple"
_REMOTE_ADDRESS = "203.0.113.10"


def _build_test_app(tmp_path: Path, *, bearer_token: str | None) -> FastAPI:
    if bearer_token is None:
        delete_config("gact.auth.bearer_token")
    else:
        set_config("gact.auth.bearer_token", bearer_token)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    app.state.peer_address_getter = lambda _scope: _REMOTE_ADDRESS
    return app


def _assert_typed_bearer_refusal(response: Any) -> None:
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    error = response.json()["error"]
    assert error["error"] == "authentication_required"
    assert error["details"]["scheme"] == "bearer"
    assert error["recoverable"] is True


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Basic dXNlcjpwYXNz"},
    ],
)
def test_remote_request_requires_configured_bearer_token(
    tmp_path: Path,
    headers: dict[str, str],
) -> None:
    app = _build_test_app(tmp_path, bearer_token=_BEARER_TOKEN)

    with TestClient(app) as client:
        response = client.get("/v1/capabilities", headers=headers)

    _assert_typed_bearer_refusal(response)


def test_remote_request_accepts_correct_bearer_token(tmp_path: Path) -> None:
    app = _build_test_app(tmp_path, bearer_token=_BEARER_TOKEN)

    with TestClient(app) as client:
        response = client.get(
            "/v1/capabilities",
            headers={"Authorization": f"Bearer {_BEARER_TOKEN}"},
        )

    assert response.status_code == 200


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_loopback_request_keeps_trust_socket_access(tmp_path: Path, address: str) -> None:
    app = _build_test_app(tmp_path, bearer_token=_BEARER_TOKEN)
    app.state.peer_address_getter = lambda _scope: address

    with TestClient(app) as client:
        response = client.get("/v1/capabilities")

    assert response.status_code == 200


def test_remote_request_is_unchanged_without_configured_token(tmp_path: Path) -> None:
    app = _build_test_app(tmp_path, bearer_token=None)

    with TestClient(app) as client:
        response = client.get("/v1/capabilities")

    assert response.status_code == 200
    assert response.json()["auth"] == {
        "schemes": ["trust_socket"],
        "current": "trust_socket",
    }


def test_capabilities_advertise_bearer_only_when_configured(tmp_path: Path) -> None:
    app = _build_test_app(tmp_path, bearer_token=_BEARER_TOKEN)

    with TestClient(app) as client:
        response = client.get(
            "/v1/capabilities",
            headers={"Authorization": f"Bearer {_BEARER_TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json()["auth"] == {
        "schemes": ["trust_socket", "bearer"],
        "current": "bearer",
    }


def test_bearer_token_uses_environment_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delete_config("gact.auth.bearer_token")
    monkeypatch.setenv("CLIO_GACT_BEARER_TOKEN", _BEARER_TOKEN)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    app.state.peer_address_getter = lambda _scope: _REMOTE_ADDRESS

    with TestClient(app) as client:
        response = client.get(
            "/v1/capabilities",
            headers={"Authorization": f"Bearer {_BEARER_TOKEN}"},
        )

    assert response.status_code == 200
    assert response.json()["auth"]["current"] == "bearer"


def test_sse_route_accepts_auth_token_query_parameter(tmp_path: Path) -> None:
    app = _build_test_app(tmp_path, bearer_token=_BEARER_TOKEN)

    with TestClient(app) as client:
        response = client.get(f"/v1/sessions/missing/events?auth_token={_BEARER_TOKEN}")

    assert response.status_code == 404
    assert response.json()["error"]["error"] == "not_found"


def test_non_sse_route_rejects_auth_token_query_parameter(tmp_path: Path) -> None:
    app = _build_test_app(tmp_path, bearer_token=_BEARER_TOKEN)

    with TestClient(app) as client:
        response = client.get(f"/v1/capabilities?auth_token={_BEARER_TOKEN}")

    _assert_typed_bearer_refusal(response)
