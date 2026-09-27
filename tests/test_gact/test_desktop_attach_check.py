"""GET /v1/desktop/attach: can an attaching desktop open the bearer-only surfaces? (#1478)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


def _client(tmp_path: Path) -> TestClient:
    app = build_app(sessions_path=tmp_path / "sessions.json")
    # The desktop attaches from loopback, where plain HTTP needs no token.
    app.state.peer_address_getter = lambda _scope: "127.0.0.1"
    return TestClient(app)


def test_a_server_that_enforces_a_token_answers_401_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_AUTH_TOKEN", "launcher-token")
    with _client(tmp_path) as client:
        assert client.get("/v1/infrastructure/targets").status_code == 200  # loopback HTTP
        response = client.get("/v1/desktop/attach")

    assert response.status_code == 401
    assert response.json()["error"]["error"] == "authentication_required"


def test_the_right_token_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIO_AUTH_TOKEN", "launcher-token")
    with _client(tmp_path) as client:
        response = client.get(
            "/v1/desktop/attach", headers={"Authorization": "Bearer launcher-token"}
        )

    assert response.status_code == 204


def test_a_server_without_a_token_lets_a_record_less_desktop_attach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLIO_GACT_BEARER_TOKEN", raising=False)
    with _client(tmp_path) as client:
        response = client.get("/v1/desktop/attach")

    assert response.status_code == 204
