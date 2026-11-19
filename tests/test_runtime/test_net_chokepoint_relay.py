"""The relay tunnel: the Linux fence's way to a private or local host, with consent."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator

import pytest

from clio_agent.runtime import net_chokepoint as nc
from clio_agent.runtime import net_connect, net_targets


def _echo_server() -> int:
    """A one-shot loopback echo server (stands in for an SSH server on a private host)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve() -> None:
        conn, _ = server.accept()
        with conn, server:
            while chunk := conn.recv(1024):
                conn.sendall(chunk)

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1]


@pytest.fixture
def proxy() -> Iterator[nc.Chokepoint]:
    nc.set_egress_gate(None)
    nc.set_egress_recorder(None)
    chokepoint = nc.Chokepoint().start()
    yield chokepoint
    chokepoint.stop()
    nc.set_egress_gate(None)
    nc.set_egress_recorder(None)


def _relay(proxy: nc.Chokepoint, host: str, port: int) -> tuple[socket.socket, bytes]:
    return net_connect.open_tunnel(
        host, port, f"http://127.0.0.1:{proxy.port}", net_connect.RELAY_AUTHORITY
    )


@pytest.mark.parametrize(
    ("ip", "network"),
    [
        ("127.0.1.1", nc.NETWORK_LOOPBACK),
        ("::1", nc.NETWORK_LOOPBACK),
        ("::ffff:10.1.2.3", nc.NETWORK_PRIVATE),
        ("169.254.1.1", nc.NETWORK_LINK_LOCAL),
        ("172.20.1.1", nc.NETWORK_PRIVATE),
        ("fd00::1", nc.NETWORK_PRIVATE),
        ("1.1.1.1", ""),
        ("2606:4700::1111", ""),
    ],
)
def test_addresses_are_classified(ip: str, network: str) -> None:
    assert net_targets._address_network(ip) == network


def test_a_host_takes_its_strictest_address_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    infos = [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.20.1.1", 22)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.1.1", 22)),
    ]
    monkeypatch.setattr(nc.socket, "getaddrinfo", lambda *_a, **_k: infos)
    addresses, network = nc._resolve_target("ares.local", 22)
    assert network == nc.NETWORK_LOOPBACK
    assert [sockaddr for _family, sockaddr in addresses] == [("172.20.1.1", 22), ("127.0.1.1", 22)]


def test_an_approved_local_host_is_reached_and_recorded(
    proxy: nc.Chokepoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(nc, "_reaches_clio_listener", lambda _upstream: False)
    asked: list[nc.EgressRecord] = []
    recorded: list[nc.EgressRecord] = []
    nc.set_egress_gate(lambda rec: asked.append(rec) or "allow")
    nc.set_egress_recorder(recorded.append)
    port = _echo_server()
    sock, first = _relay(proxy, "127.0.0.1", port)
    with sock:
        sock.sendall(b"SSH-2.0-test\r\n")
        assert first + sock.recv(64) == b"SSH-2.0-test\r\n"
    assert [(rec.host, rec.port, rec.network) for rec in asked] == [
        ("127.0.0.1", port, nc.NETWORK_LOOPBACK)
    ]
    assert [(rec.host, rec.resolved_ip, rec.network, rec.transport) for rec in recorded] == [
        ("127.0.0.1", "127.0.0.1", nc.NETWORK_LOOPBACK, "connect")
    ]


def test_a_declined_local_host_is_refused_before_any_dial(
    proxy: nc.Chokepoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    dialed: list[object] = []
    monkeypatch.setattr(nc, "_dial_addresses", lambda *a: dialed.append(a))
    nc.set_egress_gate(lambda _rec: "deny")
    with pytest.raises(net_connect.ProxyRefused) as caught:
        _relay(proxy, "127.0.0.1", 22)
    assert caught.value.status == 403
    assert dialed == []


def test_without_a_gate_a_local_host_stays_closed(proxy: nc.Chokepoint) -> None:
    with pytest.raises(net_connect.ProxyRefused) as caught:
        _relay(proxy, "127.0.0.1", 22)
    assert caught.value.status == 403


def test_a_crashing_gate_keeps_a_local_host_closed(proxy: nc.Chokepoint) -> None:
    def crash(_rec: nc.EgressRecord) -> str:
        raise RuntimeError("gate down")

    nc.set_egress_gate(crash)
    with pytest.raises(net_connect.ProxyRefused) as caught:
        _relay(proxy, "127.0.0.1", 22)
    assert caught.value.status == 403


def test_clio_own_listeners_are_never_reached(proxy: nc.Chokepoint) -> None:
    """Even an approved relay cannot reach a port this process serves (CLIO's API or proxy)."""
    recorded: list[nc.EgressRecord] = []
    nc.set_egress_gate(lambda _rec: "allow")
    nc.set_egress_recorder(recorded.append)
    with pytest.raises(net_connect.ProxyRefused) as caught:
        _relay(proxy, "127.0.0.1", proxy.port)
    assert caught.value.status == 403
    assert recorded == []


def test_a_relay_inside_the_relay_is_rejected(proxy: nc.Chokepoint) -> None:
    with pytest.raises(net_connect.ProxyRefused) as caught:
        _relay(proxy, net_connect.RELAY_HOST, net_connect.RELAY_PORT)
    assert caught.value.status == 400


def test_a_public_relayed_host_carries_no_network_kind(
    proxy: nc.Chokepoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    public = [(socket.AF_INET, ("1.1.1.1", 22))]
    monkeypatch.setattr(nc, "_resolve_target", lambda *_a: (public, ""))
    asked: list[nc.EgressRecord] = []
    nc.set_egress_gate(lambda rec: asked.append(rec) or "deny")
    with pytest.raises(net_connect.ProxyRefused):
        _relay(proxy, "public.example", 22)
    assert [(rec.host, rec.network) for rec in asked] == [("public.example", "")]
