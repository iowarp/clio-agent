"""Token-less state changes from an untrusted web page are refused (origin guard)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

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


def _refused(response, reason: str = "origin_not_allowed") -> bool:  # noqa: ANN001
    return response.status_code == 403 and (response.json().get("error", {}).get("error") == reason)


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

    assert _refused(response, "host_not_allowed")


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


# --- DNS rebinding: token-less requests must address an allowed Host ---------


def test_a_rebound_host_cannot_read_without_a_token(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="clio_agent.gact.origin_guard")

    response = client.get("/v1/sessions", headers={"Host": "evil.example"})

    assert _refused(response, "host_not_allowed"), (response.status_code, response.text)
    assert any("host_not_allowed" in record.getMessage() for record in caplog.records)


def test_a_valid_token_reads_under_any_host(app: FastAPI, client: TestClient) -> None:
    token = app.state.bearer_token
    if token is None:
        return  # nothing to present; the refusal above covers this app
    response = client.get(
        "/v1/sessions", headers={"Host": "evil.example", "Authorization": f"Bearer {token}"}
    )

    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "host", ["localhost", "localhost:8080", "127.0.0.1:18960", "[::1]:8100", "[::1]"]
)
def test_loopback_host_names_read_without_a_token(client: TestClient, host: str) -> None:
    assert client.get("/v1/sessions", headers={"Host": host}).status_code == 200


def test_a_configured_lan_host_reads_without_a_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_GACT_ALLOWED_HOSTS", "testserver,clio.lab.example")
    conf.reload()

    assert client.get("/v1/sessions", headers={"Host": "clio.lab.example:8080"}).status_code == 200
    assert _refused(
        client.get("/v1/sessions", headers={"Host": "evil.example"}), "host_not_allowed"
    )


def test_the_same_origin_web_ui_on_a_configured_host_can_change_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_GACT_ALLOWED_HOSTS", "testserver,clio.lab.example")
    conf.reload()

    response = _post_plain(
        client,
        EXPOSED_ROUTES[0],
        {"Host": "clio.lab.example:8080", "Origin": "http://clio.lab.example:8080"},
    )

    assert not _refused(response) and not _refused(response, "host_not_allowed")


def test_a_rebound_websocket_upgrade_is_refused(client: TestClient) -> None:
    with pytest.raises(WebSocketDenialResponse) as denied:
        with client.websocket_connect(
            "/v1/infrastructure/targets/local/transport", headers={"Host": "evil.example"}
        ):
            pass

    assert denied.value.status_code == 403
    assert denied.value.json()["error"]["error"] == "host_not_allowed"


def test_the_clio_web_image_proxy_host_keeps_the_same_origin_ui_working(
    client: TestClient,
) -> None:
    # docker/nginx-clio-web.conf forwards the browser's Host with its port
    # ($http_host). nginx's $host drops the port, and the SPA's POSTs (Origin
    # http://localhost:8080) would then no longer match their own origin.
    with_port = _post_plain(
        client,
        EXPOSED_ROUTES[0],
        {"Host": "localhost:8080", "Origin": "http://localhost:8080"},
    )
    without_port = _post_plain(
        client, EXPOSED_ROUTES[0], {"Host": "localhost", "Origin": "http://localhost:8080"}
    )

    assert not _refused(with_port)
    assert _refused(without_port)


def test_the_clio_web_nginx_config_forwards_host_with_port_and_scheme() -> None:
    conf_text = (Path(__file__).resolve().parents[2] / "docker" / "nginx-clio-web.conf").read_text(
        encoding="utf-8"
    )

    assert "proxy_set_header Host $host;" not in conf_text
    assert conf_text.count("proxy_set_header Host $http_host;") == 2
    assert conf_text.count("proxy_set_header X-Forwarded-Proto $scheme;") == 2
