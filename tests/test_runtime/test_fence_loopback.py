"""A fenced server's loopback listeners are attributed to its launch, not its current parents."""

from __future__ import annotations

import os
import socket
import sys
from types import SimpleNamespace

import psutil
import pytest

from clio_agent.runtime import fence_loopback
from clio_agent.runtime.fence_loopback import LoopbackPublisher, _listener_address, _Server

AGENT = 4000
FORWARD = 5000
PORT = 53551


def _listening(pid: int, port: int = PORT, ip: str = "127.0.0.1") -> SimpleNamespace:
    return SimpleNamespace(
        status=psutil.CONN_LISTEN, laddr=SimpleNamespace(ip=ip, port=port), pid=pid
    )


@pytest.fixture
def table(monkeypatch: pytest.MonkeyPatch) -> dict[int, str]:
    """A listener on PORT owned by FORWARD, outside the agent's tree; maps pid -> container."""

    containers: dict[int, str] = {AGENT: "S-1-15-2-1", FORWARD: "S-1-15-2-1"}
    monkeypatch.setattr(psutil, "net_connections", lambda kind: [_listening(FORWARD)])
    monkeypatch.setattr(fence_loopback, "_tree", lambda pid: {pid})
    monkeypatch.setattr(fence_loopback, "_container_sid", lambda pid: containers.get(pid, ""))
    return containers


def test_a_detached_listener_in_the_servers_container_is_its_own(table: dict[int, str]) -> None:
    assert _listener_address(AGENT, PORT, "S-1-15-2-1") == "127.0.0.1"


def test_a_listener_in_another_container_is_not(table: dict[int, str]) -> None:
    table[FORWARD] = "S-1-15-2-2"
    assert _listener_address(AGENT, PORT, "S-1-15-2-1") is None


def test_without_a_container_only_the_tree_counts(table: dict[int, str]) -> None:
    assert _listener_address(AGENT, PORT) is None
    assert _listener_address(FORWARD, PORT) == "127.0.0.1"


def test_a_container_listener_on_a_non_loopback_address_is_not(
    monkeypatch: pytest.MonkeyPatch, table: dict[int, str]
) -> None:
    monkeypatch.setattr(
        psutil, "net_connections", lambda kind: [_listening(FORWARD, ip="10.0.0.5")]
    )
    assert _listener_address(AGENT, PORT, "S-1-15-2-1") is None


def _server(pid: int, container: str) -> _Server:
    return _Server(token="t", pid=pid, control=socket.socket(), container=container)


def test_a_container_shared_by_two_servers_singles_out_neither() -> None:
    publisher = LoopbackPublisher()
    try:
        first, second, alone = _server(1, "S-a"), _server(2, "S-a"), _server(3, "S-b")
        publisher._servers.update({"1": first, "2": second, "3": alone})
        assert publisher._container(first) == ""
        assert publisher._container(second) == ""
        assert publisher._container(alone) == "S-b"
    finally:
        publisher._listener.close()
        for server in (first, second, alone):
            server.control.close()


@pytest.mark.skipif(sys.platform != "win32", reason="AppContainer SIDs are Windows tokens")
def test_an_unfenced_process_has_no_container() -> None:
    assert fence_loopback._container_sid(os.getpid()) == ""
    assert fence_loopback._container_sid(-1) == ""


def test_other_platforms_have_no_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert fence_loopback._container_sid(os.getpid()) == ""
