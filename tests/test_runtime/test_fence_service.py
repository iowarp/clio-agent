"""Reaching a declared loopback port inside a fenced child's network namespace."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time

import pytest

from clio_agent.runtime import fence_service
from clio_agent.runtime.fence_service import FencedNamespace

_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"


def _row(address: str, port: int, state: str = "0A") -> str:
    return f"   0: {address}:{port:04X} 00000000:0000 {state} 00000000:00000000 00:0 0 1000\n"


def test_listening_ports_reads_loopback_and_any_address_listeners() -> None:
    table = (
        _HEADER
        + _row("0100007F", 41207)  # 127.0.0.1
        + _row("00000000", 8080)  # 0.0.0.0
        + _row("0A00000A", 9000)  # 10.0.0.10: not loopback
        + _row("0100007F", 5000, state="01")  # established, not listening
    )
    assert fence_service.listening_ports(table) == {41207, 8080}


def test_listening_ports_reads_ipv6_loopback_and_mapped_ipv4() -> None:
    table = (
        _HEADER
        + _row("00000000000000000000000001000000", 7001)  # ::1
        + _row("0" * 32, 7002)  # ::
        + _row("0000000000000000FFFF00000100007F", 7003)  # ::ffff:127.0.0.1
        + _row("20010DB8000000000000000000000001", 7004)  # 2001:db8::1
    )
    assert fence_service.listening_ports(table) == {7001, 7002, 7003}


def _fake(monkeypatch: pytest.MonkeyPatch, namespaces, marks, listens) -> None:
    monkeypatch.setattr(fence_service.sys, "platform", "linux")
    monkeypatch.setattr(fence_service, "_fenced_namespaces", lambda: namespaces)
    monkeypatch.setattr(fence_service, "_carries", lambda pid, child: marks.get(pid) == child)
    monkeypatch.setattr(fence_service, "_listens", lambda pid, port: port in listens.get(pid, ()))


def test_the_serving_childs_namespace_is_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(
        monkeypatch,
        {11: [101, 102], 22: [201]},
        marks={102: "child-a", 201: "child-b"},
        listens={101: {41207}, 201: {41207}},
    )
    assert fence_service.find_fenced_namespace(41207, "child-a") == FencedNamespace(101, 11)
    assert fence_service.find_fenced_namespace(41207, "child-b") == FencedNamespace(201, 22)


def test_two_listening_namespaces_without_a_known_child_are_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake(monkeypatch, {11: [101], 22: [201]}, marks={}, listens={101: {80}, 201: {80}})
    assert fence_service.find_fenced_namespace(80) is None
    assert fence_service.find_fenced_namespace(80, "unmarked") is None


def test_an_unmarked_child_falls_back_to_the_only_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch, {11: [101], 22: [201]}, marks={}, listens={201: {80}})
    assert fence_service.find_fenced_namespace(80, "child-a") == FencedNamespace(201, 22)
    assert fence_service.find_fenced_namespace(81, "child-a") is None


def test_nothing_is_searched_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fence_service.sys, "platform", "win32")

    def boom():
        raise AssertionError("searched")

    monkeypatch.setattr(fence_service, "_fenced_namespaces", boom)
    assert fence_service.find_fenced_namespace(80) is None
    assert fence_service.fenced_services_possible() is False


_LISTENER = r"""
import fcntl, os, socket, struct, sys
# A new network namespace starts with loopback down; the fence brings it up.
probe = socket.socket()
flags = struct.unpack("16sH", fcntl.ioctl(probe, 0x8913, struct.pack("16sH", b"lo", 0)))[1]
fcntl.ioctl(probe, 0x8914, struct.pack("16sH", b"lo", flags | 1))
srv = socket.socket()
srv.bind(("127.0.0.1", 0))
srv.listen(4)
print(srv.getsockname()[1], flush=True)
while True:
    conn, _ = srv.accept()
    conn.sendall(b"hello-from-" + os.environ["CLIO_FENCE_CHILD"].encode())
    conn.close()
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="network namespaces are Linux")
def test_a_port_in_a_child_namespace_is_dialed_inside_it() -> None:
    """A real user+network namespace: the host cannot reach the port; the dial can."""

    unshare = shutil.which("unshare")
    if unshare is None:
        pytest.skip("unshare is not installed")
    env = {**os.environ, fence_service.FENCE_CHILD_VARIABLE: "child-x"}
    child = subprocess.Popen(  # noqa: S603
        [unshare, "-Urn", sys.executable, "-c", _LISTENER],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    try:
        line = child.stdout.readline() if child.stdout else ""
        if not line.strip().isdigit():
            pytest.skip(
                f"cannot create namespaces here: {child.stderr.read() if child.stderr else ''}"
            )
        port = int(line)
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
        target = None
        for _ in range(50):
            target = fence_service.find_fenced_namespace(port, "child-x")
            if target is not None:
                break
            time.sleep(0.05)
        assert target is not None
        assert target.inode != os.stat("/proc/self/ns/net").st_ino
        with fence_service.connect_in_namespace(target, port, timeout=5) as sock:
            assert sock.recv(64) == b"hello-from-child-x"
        with pytest.raises(OSError):
            fence_service.connect_in_namespace(target, port + 1 if port < 65535 else 1, 2)
    finally:
        child.kill()
        child.wait()
