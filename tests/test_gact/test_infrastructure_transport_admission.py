"""Desktop SSH transport admission: typed refusals and loopback trust (#1478)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from clio_agent.gact.app import build_app

V1 = "clio.infrastructure.v1"
V2 = "clio.infrastructure.v2"
TOKEN_PROTOCOL = "clio-bearer.YnJpZGdlLXRva2Vu"  # urlsafe base64 for bridge-token


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """A GACT app whose launcher exported a bearer token, like the Desktop's."""

    monkeypatch.setenv("CLIO_AUTH_TOKEN", "bridge-token")
    return build_app(sessions_path=tmp_path / "sessions.json")


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def ssh_target(client: TestClient) -> str:
    created = client.post(
        "/v1/infrastructure/targets",
        json={
            "label": "CHPC notch351",
            "kind": "ssh",
            "ssh": {
                "host": "notch351",
                "user": "u1282901",
                "jump_hosts": ["u1282901@notchpeak2.chpc.utah.edu"],
            },
        },
        headers={"Authorization": "Bearer bridge-token"},
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _path(target_id: str) -> str:
    return f"/v1/infrastructure/targets/{target_id}/transport"


def _refusal_after_handshake(
    client: TestClient, target_id: str, protocols: list[str]
) -> tuple[int, str]:
    """Open a v2 socket and return the (code, reason) of the close frame it gets."""

    with client.websocket_connect(_path(target_id), subprotocols=protocols) as websocket:
        assert websocket.accepted_subprotocol == V2
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()
    return closed.value.code, closed.value.reason


def test_v2_wrong_token_gets_a_typed_close_frame_not_a_bare_handshake_failure(
    client: TestClient, ssh_target: str
) -> None:
    code, reason = _refusal_after_handshake(client, ssh_target, [V2, V1, "clio-bearer.d3Jvbmc"])

    assert (code, reason) == (4401, "authentication_required")


def test_v2_unknown_target_and_non_ssh_target_are_typed(client: TestClient) -> None:
    assert _refusal_after_handshake(client, "no-such-target", [V2, TOKEN_PROTOCOL]) == (
        4404,
        "target_not_found",
    )
    assert _refusal_after_handshake(client, "local", [V2, TOKEN_PROTOCOL]) == (
        4409,
        "target_not_ssh",
    )


def test_v2_admitted_transport_announces_attachment(
    app: FastAPI, client: TestClient, ssh_target: str
) -> None:
    with client.websocket_connect(_path(ssh_target), subprotocols=[V2, V1, TOKEN_PROTOCOL]) as ws:
        assert ws.accepted_subprotocol == V2
        assert ws.receive_json() == {"type": "attached", "target_id": ssh_target}
        assert app.state.infrastructure_store.target(ssh_target).transport_state == "connected"


def test_loopback_peer_without_token_is_refused_unlike_http(
    app: FastAPI, client: TestClient, ssh_target: str
) -> None:
    # HTTP admits loopback without a token, but a cross-origin page cannot read
    # those responses. On this socket it could act as the SSH bridge, so the
    # token is required from loopback too.
    app.state.peer_address_getter = lambda _scope: "127.0.0.1"
    assert client.get("/v1/infrastructure/targets").status_code == 200

    code, reason = _refusal_after_handshake(client, ssh_target, [V2, V1])

    assert (code, reason) == (4401, "authentication_required")


def test_non_loopback_peer_without_token_is_refused(client: TestClient, ssh_target: str) -> None:
    code, reason = _refusal_after_handshake(client, ssh_target, [V2, V1])

    assert (code, reason) == (4401, "authentication_required")


def _connect_from(client: TestClient, target_id: str, origin: str) -> tuple[int, str]:
    with client.websocket_connect(
        _path(target_id), subprotocols=[V2, V1, TOKEN_PROTOCOL], headers={"Origin": origin}
    ) as websocket:
        try:
            frame = websocket.receive_json()
        except WebSocketDisconnect as closed:
            return closed.code, closed.reason
    return 1000, str(frame.get("type"))


def test_a_foreign_web_page_cannot_attach_even_with_the_token(
    client: TestClient, ssh_target: str
) -> None:
    # Browsers apply no CORS to WebSockets: any page can open this socket.
    assert _connect_from(client, ssh_target, "https://evil.example") == (
        4403,
        "origin_not_allowed",
    )


@pytest.mark.parametrize(
    "origin", ["http://tauri.localhost", "tauri://localhost", "https://tauri.localhost"]
)
def test_the_desktop_webview_origins_attach(
    client: TestClient, ssh_target: str, origin: str
) -> None:
    assert _connect_from(client, ssh_target, origin) == (1000, "attached")


def test_a_configured_cors_origin_attaches(
    client: TestClient, ssh_target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIO_GACT_CORS_ORIGINS", "https://ops.example.edu")

    assert _connect_from(client, ssh_target, "https://ops.example.edu") == (1000, "attached")
    assert _connect_from(client, ssh_target, "http://localhost:5173")[0] == 4403


def test_v1_only_client_keeps_the_pre_handshake_refusal_and_the_reason_is_logged(
    client: TestClient, ssh_target: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="clio_agent.gact.infrastructure.transport_admission")

    with pytest.raises(WebSocketDisconnect) as refusal:
        with client.websocket_connect(_path(ssh_target), subprotocols=[V1, "clio-bearer.d3Jvbmc"]):
            pass

    assert refusal.value.code == 4401
    assert any(
        "authentication_required" in record.getMessage() and ssh_target in record.getMessage()
        for record in caplog.records
    )


def test_v1_admitted_client_gets_no_attached_frame(
    app: FastAPI, client: TestClient, ssh_target: str
) -> None:
    # A v1 Desktop treats any JSON frame it does not know as a forward request;
    # the attached frame is only for clients that negotiated v2.
    with client.websocket_connect(_path(ssh_target), subprotocols=[V1, TOKEN_PROTOCOL]) as ws:
        assert ws.accepted_subprotocol == V1
        ws.send_json({"type": "exec_result", "request_id": "unknown", "result": {}})
        assert app.state.infrastructure_store.target(ssh_target).transport_state == "connected"
