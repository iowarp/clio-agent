"""Token-less state changes from an untrusted web page are refused (origin guard)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.gact.app import build_app
from clio_agent.gact.routes import sandbox_setup as sandbox_setup_routes

EVIL = "https://evil.example"

# The body-less state-changing routes a foreign page could drive before.
EXPOSED_ROUTES = [
    "/v1/sessions/sess_missing/cancel",
    "/v1/providers/no-such-provider/install",
    "/v1/system/sandbox/setup",
    "/v1/mcp/servers/no-such-server/reconnect",
    "/v1/agent-tasks/no-such-task/cancel",
]


@pytest.fixture(autouse=True)
def _fresh_conf(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("CLIO_GACT_CORS_ORIGINS", raising=False)
    conf.reload()
    yield
    conf.reload()


@pytest.fixture(params=["desktop-token", None], ids=["token-configured", "no-token"])
def app(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A GACT app reached from loopback, with and without a configured bearer."""

    if request.param is None:
        monkeypatch.delenv("CLIO_AUTH_TOKEN", raising=False)
        monkeypatch.delenv("CLIO_GACT_BEARER_TOKEN", raising=False)
    else:
        monkeypatch.setenv("CLIO_AUTH_TOKEN", request.param)
    built = build_app(sessions_path=tmp_path / "sessions.json")
    built.state.peer_address_getter = lambda _scope: "127.0.0.1"
    return built


@pytest.fixture
def client(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # Never run the real elevation flow when a request is admitted.
    monkeypatch.setattr(sandbox_setup_routes, "current_platform", lambda: "linux")
    with TestClient(app, base_url="http://127.0.0.1:18960") as test_client:
        yield test_client


def _post_plain(client: TestClient, path: str, headers: dict[str, str]):
    """A CORS "simple" request: POST, text/plain, no custom headers."""

    return client.post(path, content=b"", headers={"Content-Type": "text/plain", **headers})


def _refused(response) -> bool:  # noqa: ANN001 - httpx.Response
    return response.status_code == 403 and (
        response.json().get("error", {}).get("error") == "origin_not_allowed"
    )


@pytest.mark.parametrize("path", EXPOSED_ROUTES)
def test_a_foreign_page_cannot_change_state_without_a_token(
    client: TestClient, path: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="clio_agent.gact.origin_guard")

    response = _post_plain(client, path, {"Origin": EVIL})

    assert _refused(response), (response.status_code, response.text)
    assert any(
        "origin_not_allowed" in record.getMessage() and path in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("path", EXPOSED_ROUTES)
@pytest.mark.parametrize(
    "origin",
    [None, "http://tauri.localhost", "tauri://localhost", "http://127.0.0.1:18960"],
    ids=["no-origin", "desktop-windows", "desktop-unix", "same-origin-web"],
)
def test_trusted_callers_still_reach_the_route(
    client: TestClient, path: str, origin: str | None
) -> None:
    headers = {"Origin": origin} if origin else {}

    response = _post_plain(client, path, headers)

    assert not _refused(response), (response.status_code, response.text)


@pytest.mark.parametrize("path", EXPOSED_ROUTES)
def test_a_configured_cors_origin_still_reaches_the_route(
    client: TestClient, path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_GACT_CORS_ORIGINS", "https://ops.example.edu")
    conf.reload()

    assert not _refused(_post_plain(client, path, {"Origin": "https://ops.example.edu"}))
    assert _refused(_post_plain(client, path, {"Origin": "http://localhost:5173"}))


def test_a_cross_site_marker_without_origin_is_refused(client: TestClient) -> None:
    response = _post_plain(
        client, "/v1/sessions/sess_missing/cancel", {"Sec-Fetch-Site": "cross-site"}
    )

    assert _refused(response)


def test_a_rebound_host_name_is_not_same_origin(client: TestClient) -> None:
    # A page on evil.example resolved to 127.0.0.1 (DNS rebinding) sends a
    # matching Host and Origin, but the host name is not a loopback name.
    response = _post_plain(
        client,
        "/v1/sessions/sess_missing/cancel",
        {"Origin": "http://evil.example:18960", "Host": "evil.example:18960"},
    )

    assert _refused(response)


def test_a_foreign_get_is_left_to_cors(client: TestClient) -> None:
    response = client.get("/v1/infrastructure/targets", headers={"Origin": EVIL})

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_a_valid_token_is_not_inspected(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = app.state.bearer_token
    if token is None:
        # With no token configured nothing can be presented; covered above.
        assert _refused(_post_plain(client, EXPOSED_ROUTES[0], {"Origin": EVIL}))
        return
    response = _post_plain(
        client, EXPOSED_ROUTES[0], {"Origin": EVIL, "Authorization": f"Bearer {token}"}
    )

    assert not _refused(response), (response.status_code, response.text)
