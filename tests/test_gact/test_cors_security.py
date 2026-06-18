from __future__ import annotations

from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app


def test_default_cors_blocks_browser_preflight_for_trust_socket(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CLIO_GACT_CORS_ORIGINS", raising=False)
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))

    resp = client.options(
        "/v1/sessions",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers


def test_default_cors_does_not_authorize_browser_origin_on_trust_socket_path(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CLIO_GACT_CORS_ORIGINS", raising=False)
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))

    resp = client.post(
        "/v1/sessions",
        headers={"Origin": "https://evil.example"},
        json={"title": "browser attempt"},
    )

    # Non-browser/local clients are still allowed to use trust_socket, but a
    # browser cannot read this response or use it as an authorized CORS path.
    assert resp.status_code == 200
    assert "access-control-allow-origin" not in resp.headers


def test_configured_cors_origin_enables_trusted_browser_client(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("CLIO_GACT_CORS_ORIGINS", "http://localhost:4173")
    client = TestClient(build_app(sessions_path=tmp_path / "sessions.json"))

    preflight = client.options(
        "/v1/sessions",
        headers={
            "Origin": "http://localhost:4173",
            "Access-Control-Request-Method": "POST",
        },
    )
    created = client.post(
        "/v1/sessions",
        headers={"Origin": "http://localhost:4173"},
        json={"title": "trusted browser"},
    )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:4173"
    assert created.status_code == 200
    assert created.headers["access-control-allow-origin"] == "http://localhost:4173"
