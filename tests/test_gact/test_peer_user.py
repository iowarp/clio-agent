"""Loopback peers are admitted without a token only from the server's own account."""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent.gact.app import build_app
from clio_agent.gact.peer_user import (
    _decode_endpoint,
    loopback_peer_is_server_user,
    socket_owner_uid,
)
from tests._config_layer import delete_config, set_config

_BEARER_TOKEN = "correct-horse-battery-staple"

_TCP_HEADER = (
    "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt"
    "   uid  timeout inode\n"
)


def _row(local: str, remote: str, uid: int) -> str:
    return (
        f"   0: {local} {remote} 01 00000000:00000000 00:00000000 00000000"
        f" {uid:>5}        0 12345 1 0000000000000000 20 4 30 10 -1\n"
    )


def _app(tmp_path: Path, *, token: str | None, same_user: bool) -> FastAPI:
    if token is None:
        delete_config("gact.auth.bearer_token")
    else:
        set_config("gact.auth.bearer_token", token)
    app = build_app(sessions_path=tmp_path / "sessions.json", agent=None)
    app.state.peer_address_getter = lambda _scope: "127.0.0.1"
    app.state.peer_user_check = lambda _scope: same_user
    return app


def test_decodes_ipv4_and_ipv6_table_addresses() -> None:
    import ipaddress

    assert _decode_endpoint("0100007F:1F90") == (ipaddress.ip_address("127.0.0.1"), 8080)
    loopback6 = "00000000000000000000000001000000:0050"
    assert _decode_endpoint(loopback6) == (ipaddress.ip_address("::1"), 80)
    mapped = "0000000000000000FFFF00000100007F:0050"
    assert _decode_endpoint(mapped) == (ipaddress.ip_address("127.0.0.1"), 80)


def test_finds_the_owner_of_the_client_end(tmp_path: Path) -> None:
    import ipaddress

    tcp = tmp_path / "tcp"
    tcp.write_text(
        _TCP_HEADER
        # The server's own end of the connection, owned by the server's uid.
        + _row("0100007F:1F90", "0100007F:A000", 1000)
        # The client's end, owned by another account.
        + _row("0100007F:A000", "0100007F:1F90", 2000)
    )
    loopback = ipaddress.ip_address("127.0.0.1")
    owner = socket_owner_uid((loopback, 0xA000), (loopback, 8080), tables=(tcp,))
    assert owner == 2000
    assert socket_owner_uid((loopback, 0xA001), (loopback, 8080), tables=(tcp,)) is None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads /proc/net/tcp")
def test_a_real_connection_belongs_to_this_process_user() -> None:
    with socket.create_server(("127.0.0.1", 0)) as listener:
        server = listener.getsockname()
        with socket.create_connection(server) as client:
            accepted, peer = listener.accept()
            with accepted:
                scope = {"client": peer, "server": server}
                assert loopback_peer_is_server_user(scope) is True
                # A connection the kernel does not list is not this user's.
                closed = {"client": ("127.0.0.1", 1), "server": server}
                assert loopback_peer_is_server_user(closed) is False
        assert client


def test_an_in_process_client_has_no_socket_to_check() -> None:
    assert loopback_peer_is_server_user({"client": ("testclient", 50000)}) is True


def test_another_accounts_loopback_peer_needs_the_token(tmp_path: Path) -> None:
    app = _app(tmp_path, token=_BEARER_TOKEN, same_user=False)

    with TestClient(app) as client:
        refused = client.get("/v1/capabilities")
        admitted = client.get(
            "/v1/capabilities", headers={"Authorization": f"Bearer {_BEARER_TOKEN}"}
        )

    assert refused.status_code == 401
    assert "another account" in refused.json()["error"]["message"]
    assert admitted.status_code == 200


def test_the_servers_own_loopback_peer_needs_no_token(tmp_path: Path) -> None:
    app = _app(tmp_path, token=_BEARER_TOKEN, same_user=True)

    with TestClient(app) as client:
        assert client.get("/v1/capabilities").status_code == 200


def test_without_a_token_another_accounts_loopback_peer_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CLIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLIO_GACT_BEARER_TOKEN", raising=False)
    refused_app = _app(tmp_path, token=None, same_user=False)
    own_app = _app(tmp_path / "own", token=None, same_user=True)

    with TestClient(refused_app) as client:
        assert client.get("/v1/capabilities").status_code == 401
    with TestClient(own_app) as client:
        assert client.get("/v1/capabilities").status_code == 200
